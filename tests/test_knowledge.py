import asyncio
import json
import sys
import tempfile
import time
import unittest
import wave
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_assistant.archive import verified_backup
from wechat_assistant.discovery import set_project_chats
from wechat_assistant.knowledge import get_brief,save_brief,read_updates,receipt_current
from wechat_assistant.media import MediaReader
from wechat_assistant.retrieval import public_message,conversation
from wechat_assistant.store import Store
from wechat_assistant.work import configure_project


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root);self.now=int(time.time())
        self.project='测试资料项目';configure_project(self.store,self.project)
        self.pid=self.store.projects()[0]['id'];self.group='test:work@chatroom'
        self.first=self.add('测试资料项目：周五试用，尚未验收')
        self.second=self.add('接口负责人待确认')
        set_project_chats(self.store,self.project,[self.group])
        self.reader=MediaReader(self.root,{},self.store)
        self.reader._source=SimpleNamespace(account='test')

    def tearDown(self):self.temp.cleanup()

    def add(self,text,ts=None,kind='文本',file=False):
        number=self.store.status()['total']+1
        record=dict(uid=str(number),chat_id=self.group,chat_name='测试资料群',sender='测试同事',
            ts=self.now if ts is None else ts,kind=kind,text=text,raw='<type>6</type>' if file else text,
            locator=json.dumps(dict(account='test',type=49 if file else 1)))
        self.store.ingest('test',[record],number,self.now)
        return number

    def source(self,mid):return public_message(self.store,self.store.evidence([mid])[0])['review_source']

    def entry(self,key='delivery',text='周五试用，尚未验收',**extra):
        return dict(key=key,section='status',text=text,basis='reported',evidence_ids=[self.first],**extra)

    def save(self,entries=None,ids=None,version=0,archive=None):
        return save_brief(self.store,self.project,version,entries if entries is not None else [self.entry()],
            [self.source(mid) for mid in (ids if ids is not None else [self.first])],
            '只查看测试资料群指定消息，未确认最终验收；未读附件不能作为结论。',archive)

    def test_summary_is_versioned_and_unmentioned_open_issues_survive_updates(self):
        question=dict(key='owner',section='question',text='接口负责人待确认',basis='open')
        self.save([self.entry(),question])
        self.save([self.entry(text='群里计划周五试用，验收仍未确认')],version=1)
        current=get_brief(self.store,self.project)
        self.assertEqual(2,current['version']);self.assertEqual(2,current['total_entries'])
        self.assertEqual('周五试用，尚未验收',get_brief(self.store,self.project,version=1)['entries'][0]['text'])
        self.save([],[],version=2,archive=['owner'])
        self.assertEqual(1,get_brief(self.store,self.project)['total_entries'])
        self.assertEqual(2,get_brief(self.store,self.project,version=2)['total_entries'])
        self.assertEqual([],self.store.feedback())

    def test_reading_and_summarizing_one_message_does_not_skip_unread_neighbors(self):
        self.assertEqual(2,len(read_updates(self.store,self.project)['messages']))
        self.assertEqual(2,len(read_updates(self.store,self.project)['messages']))
        self.save()
        self.assertEqual([self.second],[m['id'] for m in read_updates(self.store,self.project)['messages']])
        self.assertEqual(2,len(conversation(self.store,self.group)['messages']))

    def test_partial_text_has_no_coverage_receipt_and_requires_full_evidence(self):
        long=self.add('重要约束'*1000)
        preview=conversation(self.store,self.group,limit=1)['messages'][0]
        self.assertTrue(preview['text_truncated']);self.assertNotIn('review_source',preview)
        entry=self.entry();entry['evidence_ids']=[long]
        with self.assertRaises(ValueError):self.save([entry],[])
        self.save([entry],[long])
        self.assertNotIn(long,{m['id'] for m in read_updates(self.store,self.project)['messages']})

    def test_late_old_message_edits_and_same_second_updates_remain_visible(self):
        self.save(ids=[self.first,self.second])
        late=self.add('延迟导入的旧决定',ts=self.now-90*86400)
        same=self.add('同秒补充约束')
        original=self.store.evidence([self.first])[0]
        original.update(text='改到下周试用',raw='改到下周试用')
        self.store.ingest('test',[original],same,self.now)
        updates=read_updates(self.store,self.project)['messages']
        self.assertEqual({self.first,late,same},{m['id'] for m in updates})
        self.assertEqual(same,updates[0]['id'])
        self.assertEqual('message_changed',next(m for m in updates if m['id']==self.first)['update_reason'])
        self.assertTrue(get_brief(self.store,self.project)['entries'][0]['needs_review'])

    def test_stale_source_and_conflicting_version_do_not_advance_coverage(self):
        source=self.source(self.first)
        original=self.store.evidence([self.first])[0];original.update(text='有变更',raw='有变更')
        self.store.ingest('test',[original],2,self.now)
        with self.assertRaises(ValueError):save_brief(self.store,self.project,0,[self.entry()],[source],'局部阅读')
        self.assertEqual(0,get_brief(self.store,self.project)['version'])
        self.save()
        with self.assertRaises(ValueError):self.save(ids=[self.second])
        self.assertIn(self.second,{m['id'] for m in read_updates(self.store,self.project)['messages']})

    def test_feedback_revocation_and_exclusion_flag_notes_for_review(self):
        fid=self.store.remember('线下已经验收',self.pid)
        entry=self.entry(text='用户补充：线下已验收',feedback_ids=[fid])
        self.save([entry]);self.store.revoke_feedback(fid)
        result=get_brief(self.store,self.project)
        self.assertTrue(result['feedback_changed']);self.assertTrue(result['entries'][0]['needs_review'])
        with self.assertRaises(ValueError):self.save([entry],version=1)
        set_project_chats(self.store,self.project,[self.group],excluded=True)
        self.assertEqual([],read_updates(self.store,self.project)['messages'])
        self.assertTrue(get_brief(self.store,self.project)['entries'][0]['needs_review'])
        with self.assertRaises(ValueError):self.save(version=1)

    def test_attachment_pages_have_exact_receipts_and_added_pages_do_not_erase_old_evidence(self):
        mid=self.add('方案文件',kind='链接/文件',file=True)
        path=self.root/'proposal.txt';path.write_text('第一段约束。'*9000,'utf-8')
        with patch.object(self.reader,'_file_path',return_value=path):
            first=self.reader.read(mid)
            self.assertEqual('partial',first['status'])
            self.assertEqual([0,1,2,3,4],first['attachment_receipt']['coverage']['indices'])
            entry=self.entry(text='方案前五段描述了约束',attachment_tokens=[first['attachment_receipt']['token']])
            entry['evidence_ids']=[mid]
            self.save([entry],[mid])
            rest=self.reader.read(mid,start=5)
        self.assertIsNotNone(receipt_current(self.store,first['attachment_receipt']['token']))
        update=next(m for m in read_updates(self.store,self.project)['messages'] if m['id']==mid)
        self.assertEqual('attachment_changed',update['update_reason'])
        result=get_brief(self.store,self.project)
        self.assertFalse(result['entries'][0]['needs_review'])
        self.assertEqual([0,1,2,3,4],result['entries'][0]['attachments'][0]['coverage']['indices'])
        with self.store.connect() as db:
            row=db.execute('SELECT detail FROM media WHERE message_id=?',(mid,)).fetchone()
            detail=json.loads(row['detail']);detail['parts']['0']='更正后的正文'
            db.execute('UPDATE media SET detail=? WHERE message_id=?',(json.dumps(detail),mid))
        self.assertIsNone(receipt_current(self.store,first['attachment_receipt']['token']))
        self.assertTrue(get_brief(self.store,self.project)['entries'][0]['needs_review'])

    def test_backup_is_not_reading_and_missing_or_zip_listing_has_no_content_receipt(self):
        mid=self.add('压缩包',kind='链接/文件',file=True)
        path=self.root/'files.zip'
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('note.txt','实际约束内容')
        with patch.object(self.reader,'_file_path',return_value=path):
            backup=self.reader.backup(mid)
            self.assertTrue(backup['verified'])
            self.assertEqual([],self.store.rows('SELECT * FROM media'))
            listing=self.reader.read(mid)
            self.assertNotIn('attachment_receipt',listing)
            content=self.reader.read(mid,member='note.txt')
            self.assertEqual('note.txt',content['attachment_receipt']['coverage']['member'])
        self.assertIn(mid,{m['id'] for m in read_updates(self.store,self.project)['messages']})
        missing=self.add('缺失文档',kind='链接/文件',file=True)
        with patch.object(self.reader,'_file_path',side_effect=FileNotFoundError('缺失')):
            self.assertNotIn('attachment_receipt',self.reader.read(missing))
        with self.assertRaises(ValueError):self.save([self.entry(attachment_tokens=['invented'])])

    def test_backups_preserve_same_name_versions_and_read_after_original_removed(self):
        path=self.root/'proposal.txt';path.write_text('第一版本约束','utf-8')
        one=self.add('方案v1',kind='链接/文件',file=True)
        with patch.object(self.reader,'_file_path',return_value=path):first=self.reader.backup(one)
        path.write_text('第二版本约束','utf-8')
        two=self.add('方案v2',kind='链接/文件',file=True)
        with patch.object(self.reader,'_file_path',return_value=path):second=self.reader.backup(two)
        self.assertNotEqual(first['path'],second['path']);path.unlink()
        # No WeChat source access is required when the exact backup is available.
        self.reader._source=None
        with patch('wechat_assistant.media.WeChatSource',side_effect=AssertionError('不应连接微信')):
            self.assertIn('第一版本',self.reader.read(one,force=True)['text'])
            self.assertIn('第二版本',self.reader.read(two,force=True)['text'])
        self.assertEqual('第一版本约束',Path(first['path']).read_text('utf-8'))

    def test_corrupt_backup_is_detected_and_can_be_repaired_from_exact_original(self):
        mid=self.add('方案',kind='链接/文件',file=True)
        path=self.root/'proposal.txt';path.write_text('可信正文','utf-8')
        with patch.object(self.reader,'_file_path',return_value=path):
            saved=self.reader.backup(mid);Path(saved['path']).write_text('损坏内容','utf-8')
            with self.assertRaises(FileNotFoundError):verified_backup(self.store,self.store.evidence([mid])[0])
            read=self.reader.read(mid,force=True)
            self.assertIn('可信正文',read['text']);self.assertIn('backup_note',read['detail'])
            repaired=self.reader.backup(mid)
        self.assertTrue(repaired['verified']);self.assertEqual('可信正文',Path(saved['path']).read_text('utf-8'))

    def test_image_and_voice_backups_remain_usable_and_preserve_quality(self):
        from PIL import Image
        for kind,type_id in [('图片',3),('语音',34)]:
            with self.subTest(kind=kind):
                mid=self.add('测试媒体',kind=kind)
                path=self.root/('image.png' if type_id==3 else 'voice.wav')
                if type_id==3:Image.new('RGB',(8,8),'blue').save(path)
                else:
                    with wave.open(str(path),'wb') as output:
                        output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(b'\0'*3200)
                with self.store.connect() as db:
                    db.execute('UPDATE messages SET locator=? WHERE id=?',(json.dumps(dict(account='test',type=type_id)),mid))
                    db.execute('INSERT INTO media VALUES (?,?,?,?,?,?,?)',
                        (mid,'ready',kind,str(path),'已识别内容',json.dumps({'quality':'thumbnail'} if type_id==3 else {}),self.now))
                backup=self.reader.backup(mid);path.unlink();self.reader._source=None
                model=SimpleNamespace(transcribe=lambda samples,**kw:([SimpleNamespace(start=0,end=.1,text='测试语音')],SimpleNamespace(duration=.1)))
                with patch.object(self.reader,'vision',return_value='测试图片'),patch('wechat_assistant.media.speech_model',return_value=model),patch('wechat_assistant.media.WeChatSource',side_effect=AssertionError('不应连接微信')):
                    result=self.reader.read(mid,force=True)
                self.assertEqual('ready',result['status']);self.assertIn('attachment_receipt',result)
                self.assertEqual('thumbnail' if type_id==3 else 'decoded_audio',backup['quality'])

    def test_message_revision_invalidates_media_and_old_backup_is_not_used_as_new_attachment(self):
        mid=self.add('旧方案',kind='链接/文件',file=True)
        path=self.root/'old.txt';path.write_text('旧正文','utf-8')
        with patch.object(self.reader,'_file_path',return_value=path):
            self.reader.backup(mid);old=self.reader.read(mid)
        message=self.store.evidence([mid])[0];message.update(raw='<type>6</type>新的索引',text='新方案')
        self.store.ingest('test',[message],mid,self.now)
        self.assertEqual([],self.store.rows('SELECT * FROM media'))
        self.assertIsNone(verified_backup(self.store,self.store.evidence([mid])[0]))
        self.assertIsNone(receipt_current(self.store,old['attachment_receipt']['token']))
        self.assertEqual(1,len(self.store.rows('SELECT * FROM attachment_backups')))

    def test_update_paging_under_budget_covers_all_messages_and_does_not_acknowledge_reads(self):
        for _ in range(6):self.add('很长的消息'*600)
        ids=[];before=None
        while True:
            result=read_updates(self.store,self.project,before_id=before,max_chars=4000)
            self.assertLessEqual(len(json.dumps(result['messages'],ensure_ascii=False)),4000)
            ids.extend(m['id'] for m in result['messages']);before=result['next_before_id']
            if before is None:break
        self.assertEqual(8,len(ids));self.assertEqual(8,len(set(ids)))
        self.assertEqual(8,read_updates(self.store,self.project)['total_pending'])

    def test_mcp_typed_brief_arguments_roundtrip(self):
        from wechat_assistant.mcp import create_server
        async def check():
            server=create_server(self.root)
            definitions={t.name:t for t in await server.list_tools()}
            self.assertIn('save_project_brief',definitions)
            result=await server.call_tool('save_project_brief',dict(project=self.project,expected_version=0,
                entries=[self.entry()],reviewed_sources=[self.source(self.first)],scope_note='只读两条中的第一条'))
            self.assertIn('version',str(result))
            self.assertEqual(1,get_brief(self.store,self.project)['version'])
        asyncio.run(check())

    def test_stdio_reads_backup_and_saves_cited_brief_without_wechat_connection(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        from wechat_assistant.service import Service
        web=Service(self.root)
        self.assertIsNone(web.snapshot()['projects'][0]['brief'])
        mid=self.add('方案附件',kind='链接/文件',file=True)
        path=self.root/'proposal.txt';path.write_text('验收要求：完成接口联调后再确认交付。','utf-8')
        with patch.object(self.reader,'_file_path',return_value=path):self.reader.backup(mid)
        path.unlink()
        async def check():
            params=StdioServerParameters(command=sys.executable,args=['-m','wechat_assistant.cli','--data-dir',str(self.root),'mcp'])
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    async def call(name,args):
                        result=await session.call_tool(name,args)
                        self.assertFalse(result.isError,str(result))
                        if name=='get_messages':return [json.loads(block.text) for block in result.content]
                        return json.loads(result.content[0].text)
                    evidence=await call('get_messages',{'ids':[mid]})
                    document=await call('read_attachment',{'message_id':mid,'force':True})
                    self.assertIn('接口联调',document['text'])
                    entry=self.entry(text='方案要求接口联调后确认交付',attachment_tokens=[document['attachment_receipt']['token']])
                    entry['evidence_ids']=[mid]
                    saved=await call('save_project_brief',dict(project=self.project,expected_version=0,entries=[entry],
                        reviewed_sources=[evidence[0]['review_source']],scope_note='读取方案文件；其他群消息尚未审阅'))
                    self.assertTrue(saved['saved'])
                    self.assertEqual(1,saved['version'])
                    visible=web.snapshot()['projects'][0]['brief']
                    self.assertEqual(saved['version'],visible['version'])
                    self.assertEqual(entry['text'],visible['entries']['delivery']['text'])
                    self.assertEqual([mid],visible['entries']['delivery']['evidence_ids'])
                    self.assertEqual('读取方案文件；其他群消息尚未审阅',visible['scope_note'])
                    self.assertEqual([],web.snapshot()['feedback'])
                    conflict=await session.call_tool('save_project_brief',dict(project=self.project,expected_version=0,
                        entries=[entry],reviewed_sources=[],scope_note='过期版本不应覆盖网页资料'))
                    self.assertTrue(conflict.isError)
                    self.assertEqual(visible,web.snapshot()['projects'][0]['brief'])
                    brief=await call('get_project_brief',{'project':self.project})
                    self.assertTrue(brief['assets'][0]['backups'][0]['matches_current_message'])
                    updates=await call('read_project_updates',{'project':self.project})
                    self.assertEqual({self.first,self.second},{m['id'] for m in updates['messages']})
        asyncio.run(check())


if __name__=='__main__':unittest.main()
