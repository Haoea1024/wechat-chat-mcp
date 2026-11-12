import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from wechat_assistant.discovery import discover_chats,find_private_chats,set_project_chats
from wechat_assistant.mcp import create_server
from wechat_assistant.retrieval import conversation,project_context,search_messages
from wechat_assistant.source import WeChatSource
from wechat_assistant.store import Store
from wechat_assistant.work import configure_project


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root);self.now=int(time.time())
        self.pid=configure_project(self.store,'测试门户',aliases=['客户登录'])['id']
        names={'portal@chatroom':'测试门户协作群','daily@chatroom':'日常交流',
               'alice':'同名成员','bob':'同名成员','quiet':'未发言成员','self':'本人','empty@chatroom':'测试门户旧群'}
        self.store.save_directory('a',names,[('portal@chatroom',p) for p in ('alice','quiet','self')],
                                  dict(at=self.now,roster_available=True,self_username='self'))
        self.add('a:portal@chatroom','明天上线',sender='alice')
        self.add('a:portal@chatroom','收到',sender='self')
        self.add('a:alice','客户登录还在联调')
        self.add('a:bob','周末活动')
        self.add('a:quiet','晚上吃饭')
        self.add('a:self','本人消息')
        self.add('a:daily@chatroom','客户登录方案有变',sender='bob')
        self.add('b:alice','客户登录',name='同名成员')

    def tearDown(self):self.temp.cleanup()

    def add(self,chat,text,sender='alice',name=None,ts=None):
        rows=self.store.rows('SELECT name FROM chats WHERE id=?',(chat,))
        name=name or (rows[0]['name'] if rows else chat)
        uid=str(self.store.status()['total']+1)
        self.store.ingest('test',[dict(uid=uid,chat_id=chat,chat_name=name,sender='同名成员',
            ts=self.now if ts is None else ts,kind='文本',text=text,raw=text,
            locator=json.dumps(dict(sender_username=sender)))],int(uid),self.now)
        return int(uid)

    def test_name_discovery_does_not_read_bodies_and_content_search_finds_missing_chats(self):
        initial=discover_chats(self.store,project=str(self.pid))
        self.assertEqual({'a:portal@chatroom','a:empty@chatroom'},{c['chat_id'] for c in initial['chats']})
        self.assertNotIn('明天上线',json.dumps(initial,ensure_ascii=False))
        self.assertNotIn('客户登录还在联调',json.dumps(initial,ensure_ascii=False))
        expanded=discover_chats(self.store,project=str(self.pid),include_content_matches=True)
        self.assertIn('a:daily@chatroom',{c['chat_id'] for c in expanded['chats']})
        self.assertEqual('a:portal@chatroom',expanded['chats'][0]['chat_id'])
        self.assertEqual(0,next(c for c in expanded['chats'] if c['chat_id']=='a:empty@chatroom')['message_count'])

    def test_member_discovery_uses_identifiers_excludes_self_and_keeps_unverified_candidates(self):
        result=find_private_chats(self.store,['a:portal@chatroom'],project=str(self.pid))
        by_id={c['chat_id']:c for c in result['chats']}
        self.assertEqual({'a:alice','a:quiet'},set(by_id))
        self.assertEqual('keyword_match',by_id['a:alice']['relevance'])
        self.assertEqual('membership_only',by_id['a:quiet']['relevance'])
        self.assertEqual('roster',by_id['a:quiet']['member_evidence'][0]['source'])
        self.assertEqual([],self.store.rows('SELECT * FROM project_chats'))
        self.assertNotIn('晚上吃饭',json.dumps(result,ensure_ascii=False))

    def test_missing_roster_uses_observed_speakers_and_reports_limit(self):
        self.store.save_directory('a',{},[],dict(at=self.now,roster_available=False,self_username='self'))
        result=find_private_chats(self.store,['a:portal@chatroom'])
        self.assertEqual(['a:alice'],[c['chat_id'] for c in result['chats']])
        self.assertFalse(result['coverage'][0]['roster_available'])
        self.assertEqual('observed_speaker',result['chats'][0]['member_evidence'][0]['source'])

    def test_confirmed_associations_and_exclusions_persist_across_tools(self):
        set_project_chats(self.store,str(self.pid),['a:daily@chatroom'])
        self.assertIn('a:daily@chatroom',{c['chat_id'] for c in discover_chats(Store(self.root),project=str(self.pid))['chats']})
        set_project_chats(self.store,str(self.pid),['a:alice'],excluded=True)
        self.assertNotIn('a:alice',{c['chat_id'] for c in find_private_chats(self.store,['a:portal@chatroom'],project=str(self.pid))['chats']})
        self.assertNotIn('a:alice',{m['chat_id'] for m in search_messages(self.store,project=str(self.pid))['messages']})
        self.assertEqual([],project_context(self.store,str(self.pid),chat_ids=['a:alice'])['messages'])
        with self.assertRaises(ValueError):set_project_chats(self.store,str(self.pid),['a:quiet','missing'])
        self.assertFalse(self.store.rows("SELECT * FROM project_chats WHERE chat_id='a:quiet'"))

    def test_metadata_pagination_and_literal_query(self):
        result=discover_chats(self.store,query='测试门户',limit=1)
        second=discover_chats(self.store,query='测试门户',offset=result['next_offset'],limit=1)
        self.assertNotEqual(result['chats'][0]['chat_id'],second['chats'][0]['chat_id'])
        self.assertIsNone(second['next_offset'])
        self.add('a:bob','完成 100%_验收')
        self.assertEqual(1,search_messages(self.store,query='%_')['total_matches'])
        self.assertEqual(1,discover_chats(self.store,query='%_',include_content_matches=True)['total'])

    def test_bounded_pages_never_skip_same_second_messages_or_inject_cached_attachments(self):
        ids={self.add('a:portal@chatroom','测试门户 '+('长正文'*1500)) for _ in range(7)}
        with self.store.connect() as db:
            db.execute('INSERT INTO media(message_id,status,text,detail) VALUES (?,?,?,?)',
                       (min(ids),'ready','不应自动返回的附件'*4000,'{}'))
        collected=[];offset=0
        while True:
            result=search_messages(self.store,query='测试门户',chat_ids=['a:portal@chatroom'],offset=offset,max_chars=4000)
            self.assertLessEqual(len(json.dumps(result['messages'],ensure_ascii=False)),4000)
            self.assertNotIn('不应自动返回的附件',json.dumps(result,ensure_ascii=False))
            self.assertTrue(all(m['text_truncated'] for m in result['messages']))
            collected.extend(m['id'] for m in result['messages'])
            offset=result['next_offset']
            if offset is None:break
        self.assertEqual(ids,set(collected));self.assertEqual(len(ids),len(collected))
        before=None;collected=[]
        while True:
            result=conversation(self.store,'a:portal@chatroom',before,max_chars=4000)
            collected.extend(m['id'] for m in result['messages']);before=result['next_before_id']
            if before is None:break
        self.assertEqual(len(ids)+2,len(collected));self.assertEqual(len(collected),len(set(collected)))

    def test_project_pages_do_not_expand_neighbors_and_respect_actual_consumed_count(self):
        ids={self.add('a:portal@chatroom','测试门户 '+('长'*2300)) for _ in range(4)}
        found=[];offset=0
        while True:
            result=project_context(self.store,str(self.pid),offset=offset,max_chars=4000,chat_ids=['a:portal@chatroom'])
            found.extend(m['id'] for m in result['messages']);offset=result['retrieval']['next_offset']
            if offset is None:break
        self.assertEqual(ids|{1,2},set(found));self.assertEqual(len(ids)+2,len(found))
        result=project_context(self.store,str(self.pid),limit=1)
        self.assertEqual(1,len(result['messages']))
        self.assertNotIn('a:portal@chatroom',{m['chat_id'] for m in project_context(self.store,str(self.pid),chat_ids=[])['messages']})

    def test_time_window_keeps_same_timestamp_and_rejects_wrong_conversation_cursor(self):
        old=self.add('a:portal@chatroom','旧记录',ts=self.now-20*86400)
        result=conversation(self.store,'a:portal@chatroom',since_ts=self.now,until_ts=self.now)
        self.assertEqual({1,2},{m['id'] for m in result['messages']})
        self.assertNotIn(old,{m['id'] for m in result['messages']})
        with self.assertRaises(ValueError):conversation(self.store,'a:alice',before_id=1)
        with self.assertRaises(ValueError):find_private_chats(self.store,['a:alice'])

    def test_source_roster_snapshot_replaces_removed_members_and_survives_missing_schema(self):
        path=self.root/'contact.db'
        with closing(sqlite3.connect(path)) as db,db:
            db.executescript('CREATE TABLE contact(id INTEGER,username TEXT); CREATE TABLE chatroom_member(room_id INTEGER,member_id INTEGER);')
            db.executemany('INSERT INTO contact VALUES (?,?)',[(1,'portal@chatroom'),(2,'alice'),(3,'quiet')])
            db.executemany('INSERT INTO chatroom_member VALUES (1,?)',[(2,),(3,)])
        source=WeChatSource.__new__(WeChatSource)
        source.account='a';source.names={'portal@chatroom':'测试门户协作群','alice':'成员'}
        source.identity={'username':'self'};source.cache=SimpleNamespace(get=lambda key:str(path))
        self.assertTrue(source.sync_directory(self.store,self.now)['roster_available'])
        with closing(sqlite3.connect(path)) as db,db:db.execute('DELETE FROM chatroom_member WHERE member_id=3')
        source.sync_directory(self.store,self.now)
        self.assertEqual(['a:alice'],[r['member_chat_id'] for r in self.store.rows('SELECT * FROM chat_members')])
        with closing(sqlite3.connect(path)) as db,db:db.execute('DROP TABLE chatroom_member')
        self.assertFalse(source.sync_directory(self.store,self.now)['roster_available'])
        self.assertEqual([],self.store.rows('SELECT * FROM chat_members'))

    def test_mcp_exposes_new_workflow_and_calls_real_retrieval(self):
        async def check():
            server=create_server(self.root)
            definitions={t.name:t for t in await server.list_tools()}
            for name in ('discover_chats','find_private_chats','set_project_chats','search_messages'):
                self.assertIn(name,definitions)
            self.assertIn('discover_chats',server.instructions)
            result=await server.call_tool('discover_chats',{'query':'测试门户'})
            self.assertIn('a:portal@chatroom',str(result))
            self.assertNotIn('明天上线',str(result))
            result=await server.call_tool('find_private_chats',{'group_ids':['a:portal@chatroom'],'query':'客户登录'})
            self.assertIn('keyword_match',str(result))
        asyncio.run(check())

    def test_stdio_client_discovers_groups_members_and_scoped_messages(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        async def check():
            params=StdioServerParameters(command=sys.executable,
                args=['-m','wechat_assistant.cli','--data-dir',str(self.root),'mcp'])
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    initialized=await session.initialize()
                    self.assertIn('discover_chats',initialized.instructions)
                    for tool,args in [
                        ('discover_chats',dict(query='测试门户')),
                        ('find_private_chats',dict(group_ids=['a:portal@chatroom'],query='客户登录')),
                        ('search_messages',dict(query='客户登录',chat_ids=['a:alice']))]:
                        result=await session.call_tool(tool,args)
                        self.assertFalse(result.isError,str(result))
                        data=json.loads(result.content[0].text)
                        if tool=='search_messages':
                            self.assertEqual(['a:alice'],[m['chat_id'] for m in data['messages']])
                        else:self.assertTrue(data['chats'])
        asyncio.run(check())


if __name__=='__main__':unittest.main()
