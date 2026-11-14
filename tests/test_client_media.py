import asyncio
import base64
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image

from wechat_assistant.archive import copy_attachment
from wechat_assistant.client_media import tool_result
from wechat_assistant.config import load, save
from wechat_assistant.knowledge import receipt_current, save_brief
from wechat_assistant.media import MediaReader
from wechat_assistant.service import Service
from wechat_assistant.setup_client import server_spec, use_current_client, register_codex
from wechat_assistant.speech import speech_model, prepare
from wechat_assistant.store import Store
from wechat_assistant.work import configure_project


class ClientMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.reader = MediaReader(self.root, {'provider': 'client'}, self.store)

    def attach(self, path, kind=49):
        mid = self.store.status()['total'] + 1
        self.store.ingest('test', [dict(uid=str(mid), chat_id='test:person', chat_name='测试会话',
                          sender='测试人员', ts=int(time.time()), kind='图片' if kind==3 else '文件',
                          text='测试附件', raw='测试附件', locator=json.dumps({'account':'test','type':kind}))], mid, int(time.time()))
        copy_attachment(self.store, self.store.evidence([mid])[0], path, 'original_file')
        return mid

    def picture(self, name='sample.png', color='green'):
        path = self.root / name
        Image.new('RGB', (100, 80), color).save(path)
        return path

    def test_image_is_native_content_without_external_model_and_receipt_invalidates(self):
        mid = self.attach(self.picture(), 3)
        with patch('wechat_assistant.model.Model.call', side_effect=AssertionError('No extra model')):
            result = self.reader.read(mid)
        self.assertEqual('prepared', result['status'])
        self.assertEqual('', result['text'])
        protocol = tool_result(result)
        self.assertEqual(['text','image'], [c.type for c in protocol.content])
        with Image.open(io.BytesIO(base64.b64decode(protocol.content[1].data))) as img:
            self.assertEqual((0,128,0), img.getpixel((0,0)))
        token = result['attachment_receipt']['token']
        self.assertIsNotNone(receipt_current(self.store, token))
        configure_project(self.store,'测试项目')
        save_brief(self.store,'测试项目',0,[dict(key='picture',section='status',basis='reported',text='测试图为绿色',attachment_tokens=[token])],[], '仅实际查看测试图')
        self.assertEqual('测试图为绿色',Service(self.root).snapshot()['projects'][0]['brief']['entries']['picture']['text'])
        Path(result['images'][0]['path']).write_bytes(b'changed')
        self.assertIsNone(receipt_current(self.store, token))
        with self.assertRaises(ValueError):tool_result(result)

    def test_document_pages_and_images_are_bounded_and_previous_receipt_survives(self):
        from pptx import Presentation
        from pptx.util import Inches
        ppt = Presentation()
        for number in range(2):
            slide = ppt.slides.add_slide(ppt.slide_layouts[6])
            for i in range(3):
                slide.shapes.add_picture(str(self.picture(f'pic{i}.png',['red','green','blue'][i])), Inches(i), 0)
        path = self.root / 'slides.pptx'
        ppt.save(path)
        mid = self.attach(path)
        first = self.reader.read(mid)
        self.assertEqual(2,len(first['images']))
        self.assertEqual(2,first['detail']['next_visual_offset'])
        self.assertEqual(1,first['detail']['next_start'])
        self.assertEqual([1,2],first['attachment_receipt']['coverage']['image_indices'])
        rest = self.reader.read(mid,visual_offset=2)
        self.assertEqual([3],[i['index'] for i in rest['images']])
        self.assertIsNone(rest['detail']['next_visual_offset'])
        second = self.reader.read(mid,start=1)
        self.assertIsNone(second['detail']['next_start'])
        self.assertIsNotNone(receipt_current(self.store,first['attachment_receipt']['token']))
        self.assertEqual('error',self.reader.read(mid,visual_offset=10)['status'])

    def test_zip_listing_has_no_receipt_and_member_pages_do(self):
        path = self.root/'test.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('folder/test.txt','甲'*8000+'乙')
        mid=self.attach(path)
        self.assertNotIn('attachment_receipt',self.reader.read(mid))
        first=self.reader.read(mid,member='folder/test.txt')
        self.assertEqual('partial',first['status'])
        self.assertEqual('甲'*8000,first['text'])
        second=self.reader.read(mid,start=1,member='folder/test.txt')
        self.assertEqual('乙',second['text'])
        self.assertIsNotNone(receipt_current(self.store,first['attachment_receipt']['token']))

    def test_real_stdio_transports_image_and_structured_metadata(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        mid=self.attach(self.picture(),3)
        async def run():
            async with stdio_client(StdioServerParameters(**server_spec(self.root))) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool('read_attachment',{'message_id':mid})
                    self.assertFalse(result.isError)
                    self.assertEqual(['text','image'],[c.type for c in result.content])
                    self.assertEqual(mid,result.structuredContent['message_id'])
        asyncio.run(run())

    def test_install_migration_preserves_projects_and_web_hands_off_without_model(self):
        _,config=load(self.root)
        config.update(provider='codex',background_analysis=True,projects=[{'id':1,'name':'测试项目','aliases':[]}])
        save(self.root,config)
        original=(self.root/'config.json').read_bytes()
        migration=use_current_client(self.root)
        self.assertEqual(original,Path(migration['config_backup']).read_bytes())
        service=Service(self.root)
        self.assertEqual('测试项目',service.store.projects()[0]['name'])
        self.assertFalse(service.config['background_analysis'])
        with patch('wechat_assistant.model.Model.call',side_effect=AssertionError('No extra model')):
            self.assertTrue(service.submit('analyze')['handoff'])
            self.assertTrue(service.perform('ask',{'question':'目前进度？'})['handoff'])
        self.assertTrue(service.jobs.empty())

    def test_codex_registration_preserves_comments_disabled_state_and_backup(self):
        import tomlkit
        path=self.root/'config.toml'
        original='# my settings\nmodel="keep"\n[mcp_servers.other]\ncommand="other"\n[mcp_servers.wechat-chat]\ncommand="old/wechat-assistant.exe"\nenabled=false\n'
        path.write_text(original,'utf-8')
        result=register_codex(path,{'command':'new/wechat-assistant.exe','args':['mcp'],'env':{'PYTHONIOENCODING':'utf-8'}})
        self.assertEqual(original,Path(result['backup']).read_text('utf-8'))
        text=path.read_text('utf-8');doc=tomlkit.parse(text)
        self.assertIn('# my settings',text)
        self.assertEqual('other',doc['mcp_servers']['other']['command'])
        self.assertFalse(doc['mcp_servers']['wechat-chat']['enabled'])
        self.assertEqual(900,doc['mcp_servers']['wechat-chat']['tool_timeout_sec'])

    def test_speech_download_is_install_only_and_runtime_is_offline(self):
        speech_model.cache_clear()
        self.addCleanup(speech_model.cache_clear)
        with patch('faster_whisper.WhisperModel',side_effect=OSError('not cached')) as model:
            with self.assertRaisesRegex(RuntimeError,'prepare-media'):
                speech_model(str(self.root/'models'),'small')
            self.assertTrue(model.call_args.kwargs['local_files_only'])
        model=MagicMock()
        model.transcribe.return_value=(iter([]),None)
        with patch('faster_whisper.utils.download_model',side_effect=[OSError('not cached'),'cached']) as download,patch('faster_whisper.WhisperModel',return_value=model) as construct:
            self.assertEqual('ready',prepare(self.root,{})['speech'])
            self.assertEqual(2,download.call_count)
            download.assert_called_with('small',cache_dir=str(self.root/'models'))
            self.assertTrue(construct.call_args.kwargs['local_files_only'])
            model.transcribe.assert_called_once()


if __name__=='__main__':unittest.main()
