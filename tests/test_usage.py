import asyncio
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

from mcp.types import CallToolResult,ImageContent,TextContent
from wechat_assistant.config import load
from wechat_assistant.mcp import create_server
from wechat_assistant.model import Model
from wechat_assistant.store import Store
from wechat_assistant.usage import record,result_size,summary,text_size


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);load(self.root);self.store=Store(self.root)

    def test_images_and_structured_copy_do_not_inflate_text_estimate(self):
        blocks=[TextContent(type='text',text='测试abcd'),ImageContent(type='image',data='A'*40000,mimeType='image/png')]
        self.assertEqual((6,3,1),result_size((blocks,{'duplicate':'测试abcd'})))
        self.assertEqual((6,3,1),result_size(CallToolResult(content=blocks,structuredContent={'duplicate':'测试abcd'})))
        self.assertEqual((0,0),text_size(''))

    def test_beijing_day_windows_persist_and_concurrent_counts_are_not_lost(self):
        # UTC November 15 16:01 is November 16 in Beijing.
        moment=datetime(2026,11,15,16,1,tzinfo=timezone.utc)
        record(self.store,'mcp','get_messages',now=datetime(2026,11,15,15,59,tzinfo=timezone.utc),calls=2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:record(self.store,'mcp','get_messages',now=moment,calls=1,output_chars=10),range(12)))
        reopened=Store(self.root)
        self.assertEqual(12,summary(reopened,1,now=moment)['mcp']['calls'])
        self.assertEqual(14,summary(reopened,7,now=moment)['mcp']['calls'])
        self.assertEqual(120,summary(reopened,0,now=moment)['mcp']['output_chars'])
        for days in (-1,366):
            with self.assertRaises(ValueError):summary(reopened,days)

    def test_real_stdio_counts_repeated_reads_and_errors_without_storing_contents(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        import sys
        secret='虚构敏感查询不应进入统计'
        async def check():
            params=StdioServerParameters(command=sys.executable,args=['-m','wechat_assistant.cli','--data-dir',str(self.root),'mcp'])
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    for _ in range(2):
                        result=await session.call_tool('discover_chats',{'query':secret})
                        self.assertFalse(result.isError)
                    bad=await session.call_tool('usage_stats',{'days':-1})
                    self.assertTrue(bad.isError)
                    result=await session.call_tool('usage_stats',{'days':1})
                    view=json.loads(result.content[0].text)
                    self.assertEqual(3,view['mcp']['calls']) # Excludes the in-flight stats call.
                    self.assertEqual(1,view['mcp']['errors'])
                    self.assertIsNone(view['client_quota'])
        asyncio.run(check())
        data=summary(Store(self.root),1)
        self.assertEqual(4,data['mcp']['calls'])
        self.assertNotIn(secret,json.dumps(self.store.rows('SELECT * FROM usage_daily'),ensure_ascii=False))
        self.assertEqual(0,self.store.status()['total'])

    def test_api_usage_survives_invalid_answer_and_missing_usage_is_unknown(self):
        config=dict(api_base='https://example.invalid/v1',model='test',api_key_env='TEST_UNUSED_KEY')
        model=Model(self.root,config)
        receipt=dict(choices=[{'message':{'content':'{"answer":"ok"}'}}],
                     usage={'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':50}})
        def response(value):return io.BytesIO(json.dumps(value).encode())
        with patch('urllib.request.urlopen',return_value=response(receipt)) as request:
            self.assertEqual('ok',model._api('不保存的虚构原文',{})['answer'])
            request.assert_called_once()
        receipt['choices'][0]['message']['content']='not json'
        with patch('urllib.request.urlopen',return_value=response(receipt)):
            with self.assertRaises(ValueError):model._api('第二次原文',{})
        with patch('urllib.request.urlopen',side_effect=OSError('network')):
            with self.assertRaises(OSError):model._api('第三次原文',{})
        stats=summary(self.store)['model']
        self.assertEqual((3,2,1,2),(stats['calls'],stats['reported_calls'],stats['unreported_calls'],stats['errors']))
        self.assertEqual((200,40),(stats['input_tokens'],stats['output_tokens'])) # Cached tokens are already included.
        stored=json.dumps(self.store.rows('SELECT * FROM usage_daily'),ensure_ascii=False)
        self.assertNotIn('原文',stored)

    def test_zero_reported_usage_differs_from_unknown_and_bad_usage_is_not_trusted(self):
        from wechat_assistant.usage import reported_usage
        self.assertEqual({},reported_usage({'prompt_tokens':True,'completion_tokens':-1}))
        self.assertIsNone(summary(self.store)['model']['input_tokens'])
        record(self.store,'api','model_call',calls=1,**reported_usage({'prompt_tokens':0,'completion_tokens':0}))
        self.assertEqual(0,summary(self.store)['model']['input_tokens'])
        self.assertEqual(1,summary(self.store)['model']['reported_calls'])

    def test_statistics_write_failure_does_not_break_tool_result(self):
        server=create_server(self.root)
        with patch.object(Store,'connect',side_effect=OSError('disk failure')):
            # A no-I/O test tool still returns even if recording fails.
            @server.tool()
            def meter_fixture() -> dict:return {'ok':True}
            result=asyncio.run(server.call_tool('meter_fixture',{}))
        self.assertIn('true',str(result).lower())


if __name__=='__main__':unittest.main()
