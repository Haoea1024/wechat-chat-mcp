import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from wechat_assistant.config import load
from wechat_assistant.discovery import set_project_chats
from wechat_assistant.knowledge import read_updates
from wechat_assistant.query_reads import start,finish,TTL
from wechat_assistant.retrieval import public_message,search_messages,attachments
from wechat_assistant.store import Store
from wechat_assistant.work import configure_project


class QueryReadsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);load(self.root);self.store=Store(self.root)
        self.project='虚构门户';configure_project(self.store,self.project)
        self.chat='fixture:group'
        self.add('虚构门户：周五上线，测试未完成。\n保留  空格与换行。')
        set_project_chats(self.store,self.project,[self.chat])

    def add(self,text,kind='文本',raw=None):
        mid=self.store.status()['total']+1
        self.store.ingest('fixture',[dict(uid=str(mid),chat_id=self.chat,chat_name='虚构群',sender='测试同事',
            ts=int(time.time()),kind=kind,text=text,raw=raw or text)],mid,int(time.time()))
        return mid

    def messages(self,ids):return [public_message(self.store,m) for m in self.store.evidence(ids)]

    def test_query_scope_survives_process_restart_but_never_hides_new_question(self):
        query=start(self.store);original=self.messages([1])
        self.assertEqual(original,finish(self.store,original,query))
        repeated=finish(Store(self.root),original,query)
        self.assertTrue(repeated[0]['already_returned']);self.assertNotIn('review_source',repeated[0])
        self.assertNotIn('text',repeated[0])
        self.assertEqual(original,finish(self.store,original,start(self.store)))
        self.assertEqual(original,finish(self.store,original,query,force=True))
        self.assertEqual(original,finish(self.store,original))
        self.assertEqual(1,read_updates(self.store,self.project)['total_pending'])
        self.assertEqual([],self.store.rows('SELECT * FROM project_reviewed'))
        saved=json.dumps(self.store.rows('SELECT * FROM query_reads'),ensure_ascii=False)
        self.assertNotIn('上线',saved)

    def test_changed_message_and_attachment_state_are_returned_again(self):
        query=start(self.store);finish(self.store,self.messages([1]),query)
        with self.store.connect() as db:db.execute("UPDATE messages SET text='上线取消，待重新确认' WHERE id=1")
        self.assertEqual('上线取消，待重新确认',finish(self.store,self.messages([1]),query)[0]['text'])
        self.assertTrue(finish(self.store,self.messages([1]),query)[0]['already_returned'])
        with self.store.connect() as db:
            db.execute('INSERT INTO media(message_id,status,text,detail) VALUES (1,?,?,?)',('ready','变更附件','{}'))
        self.assertIn('text',finish(self.store,self.messages([1]),query)[0])

    def test_truncation_and_page_budget_never_mark_unreturned_messages(self):
        long_id=self.add('虚构门户 '+'长文'*1600)
        for _ in range(5):self.add('虚构门户 '+('短文'*800))
        query=start(self.store);ids=[];offset=0
        while True:
            page=search_messages(self.store,query=self.project,offset=offset,max_chars=4000)
            actual=finish(self.store,page,query)
            self.assertEqual(page['next_offset'],actual['next_offset'])
            self.assertLessEqual(len(json.dumps(actual['messages'],ensure_ascii=False)),4000)
            ids.extend(m['id'] for m in actual['messages'])
            offset=actual['next_offset']
            if offset is None:break
        self.assertEqual(set(range(1,8)),set(ids));self.assertEqual(7,len(ids))
        self.assertFalse(self.store.rows('SELECT * FROM query_reads WHERE query_id=? AND message_id=?',(query,long_id)))
        full=finish(self.store,self.messages([long_id]),query)
        self.assertEqual(3205,len(full[0]['text']))
        self.assertTrue(finish(self.store,self.messages([long_id]),query)[0]['already_returned'])

    def test_expired_query_requires_fresh_context_and_prunes_only_expired_reads(self):
        old=start(self.store);finish(self.store,self.messages([1]),old)
        with self.store.connect() as db:db.execute('UPDATE query_sessions SET created=? WHERE id=?',(int(time.time())-TTL,old))
        with self.assertRaises(ValueError):finish(self.store,self.messages([1]),old)
        fresh=start(self.store)
        self.assertEqual([],self.store.rows('SELECT * FROM query_reads WHERE query_id=?',(old,)))
        self.assertEqual(self.messages([1]),finish(self.store,self.messages([1]),fresh))
        with self.assertRaises(ValueError):finish(self.store,[],'unknown')

    def test_attachment_catalog_is_bounded_and_does_not_inject_cached_body(self):
        ids=[self.add('附件说明'*700,'链接/文件','<type>6</type>') for _ in range(5)]
        with self.store.connect() as db:
            db.execute('INSERT INTO media(message_id,status,text,detail) VALUES (?,?,?,?)',(ids[0],'ready','不应顺带返回的附件全文'*1000,'{}'))
        seen=[];offset=0
        while True:
            page=attachments(self.store,offset=offset,max_chars=4000)
            self.assertLessEqual(len(json.dumps(page['messages'],ensure_ascii=False)),4000)
            self.assertNotIn('不应顺带返回',json.dumps(page,ensure_ascii=False))
            seen.extend(m['id'] for m in page['messages']);offset=page['next_offset']
            if offset is None:break
        self.assertEqual(set(ids),set(seen));self.assertEqual(len(ids),len(seen))

    def test_real_stdio_reuses_cross_tool_evidence_and_keeps_exact_originals(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        for _ in range(6):self.add('虚构门户 '+('完整证据内容。'*100))
        async def check():
            params=StdioServerParameters(command=sys.executable,args=['-m','wechat_assistant.cli','--data-dir',str(self.root),'mcp'])
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    async def call(name,args):
                        result=await session.call_tool(name,args)
                        self.assertFalse(result.isError,str(result))
                        values=[json.loads(b.text) for b in result.content if b.type=='text']
                        return (values if name in {'get_messages','list_projects'} else values[0]),sum(len(b.text) for b in result.content if b.type=='text')
                    state,_=await call('project_status',{});query=state['query_id']
                    first,first_size=await call('search_messages',dict(query=self.project,query_id=query))
                    neighbor=self.add('补充上下文：测试延期，先不要上线。')
                    second,second_size=await call('read_conversation',dict(chat_id=self.chat,query_id=query))
                    self.assertEqual({neighbor}|{m['id'] for m in first['messages']},{m['id'] for m in second['messages']})
                    self.assertTrue(all(m['already_returned'] for m in second['messages'] if m['id']!=neighbor))
                    self.assertEqual('补充上下文：测试延期，先不要上线。',next(m for m in second['messages'] if m['id']==neighbor)['text'])
                    self.assertLess(second_size,first_size*.3)
                    forced,_=await call('get_messages',dict(ids=[1],query_id=query,force=True))
                    self.assertEqual(self.store.evidence([1])[0]['text'],forced[0]['text'])
                    projects,_=await call('list_projects',{})
                    self.assertEqual({'id','name','status'},set(projects[0]))
                    detailed,_=await call('list_projects',{'include_details':True})
                    self.assertIn('items',detailed[0])
                    updates,_=await call('read_project_updates',dict(project=self.project,query_id=query))
                    self.assertEqual(8,updates['total_pending'])
                    self.assertTrue(all(m['already_returned'] for m in updates['messages']))
                    print(f'Synthetic overlap plus new context: {first_size} -> {second_size} returned characters')
        asyncio.run(check())


if __name__=='__main__':unittest.main()
