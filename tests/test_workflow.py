import copy
import hashlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from wechat_assistant.analysis import CN, analyze, apply_analysis
from wechat_assistant.config import DEFAULTS
from wechat_assistant.notify import deliver, make_notifications
from wechat_assistant.source import WeChatSource
from wechat_assistant.store import Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.now = int(time.time())
        rows = [dict(uid=str(i), chat_id='test:research@chatroom', chat_name='测试项目群',
                     sender='小周', ts=self.now, kind='文本', text=f'项目审批流程 {i}', raw=f'项目审批流程 {i}')
                for i in range(3)]
        self.store.ingest('test', rows, 3, self.now)
        self.messages = {m['id']: m for m in self.store.evidence([1, 2, 3])}
        self.proposal = {'projects': [dict(id=0, name='科研项目', summary='审批待联调', evidence=[1], items=[
            dict(id=0, title='审批联调', kind='task', owner='小周', due='', priority='normal',
                 reason='尚未联调', state='open', evidence=[1])])]}

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, proposal=None):
        return apply_analysis(self.store, proposal or self.proposal, self.messages, [1, 2, 3])

    def track(self):
        self.apply()
        self.store.update_project(1, {'status': 'tracked'})

    def test_replay_does_not_duplicate_or_reopen_manual_completion(self):
        self.apply()
        initial = len(self.store.rows('SELECT * FROM changes'))
        self.apply()
        self.assertEqual(initial, len(self.store.rows('SELECT * FROM changes')))
        with self.store.connect() as db:
            db.execute("UPDATE items SET state='done'")
        self.apply()
        self.assertEqual('done', self.store.projects()[0]['items'][0]['state'])
        self.assertEqual(0, self.store.status()['pending'])

    def test_invalid_evidence_rolls_back_and_does_not_acknowledge(self):
        invalid = copy.deepcopy(self.proposal)
        invalid['projects'][0]['items'][0]['evidence'] = [999]
        with self.assertRaises(ValueError):
            self.apply(invalid)
        self.assertEqual([], self.store.projects())
        self.assertEqual(3, self.store.status()['pending'])

    def test_bad_item_id_rolls_back_project_insertion(self):
        invalid = copy.deepcopy(self.proposal)
        invalid['projects'][0]['items'][0]['id'] = 999
        with self.assertRaises(ValueError):
            self.apply(invalid)
        self.assertEqual([], self.store.projects())
        self.assertEqual(3, self.store.status()['pending'])

    def test_model_failure_keeps_pending(self):
        class FailedModel:
            def call(self, *args):
                raise RuntimeError('network unavailable')
        with self.assertRaises(RuntimeError):
            analyze(self.store, FailedModel(), DEFAULTS)
        self.assertEqual(3, self.store.status()['pending'])

    def test_overview_refresh_preserves_items_and_does_not_notify_rewording(self):
        self.track()
        count = len(self.store.rows('SELECT * FROM changes'))
        p = self.proposal['projects'][0]
        p['id'] = 1
        p['summary'] = '审批进入联调阶段，推测下一步是验收。'
        p['overview'] = dict(next_steps=['建议先联调'], uncertainties=['验收人需要确认'],
                             conversations=[dict(chat_id='test:research@chatroom', summary='正在联调。', evidence=[1])])
        p['items'][0]['state'] = 'reported_done'
        apply_analysis(self.store, self.proposal, self.messages, [], overview_only=True)
        saved = self.store.projects()[0]
        self.assertEqual('正在联调。', saved['overview']['conversations'][0]['summary'])
        self.assertEqual('open', saved['items'][0]['state'])
        self.assertEqual(count, len(self.store.rows('SELECT * FROM changes')))

    def test_conversation_overview_cannot_cite_another_chat(self):
        self.proposal['projects'][0]['overview'] = dict(next_steps=[],uncertainties=[],
            conversations=[dict(chat_id='wrong-chat',summary='错误关联',evidence=[1])])
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(3, self.store.status()['pending'])

    def test_attached_file_changes_trigger_analysis_without_messages(self):
        self.track()
        file = self.root / 'STATUS.md'
        file.write_text('审批尚未接入', 'utf-8')
        self.store.update_project(1, {'paths': [str(file)]})
        calls = []
        class Model:
            def call(self, *args):
                calls.append(args)
                return {'projects': []}
        analyze(self.store, Model(), DEFAULTS)
        analyze(self.store, Model(), DEFAULTS)
        self.assertEqual(1, len(calls))
        file.write_text('审批已接入，等待验收', 'utf-8')
        analyze(self.store, Model(), DEFAULTS)
        self.assertEqual(2, len(calls))
        self.assertIn('审批已接入', str(calls[-1]))

    def test_excluded_chat_does_not_change_project(self):
        self.track()
        with self.store.connect() as db:
            db.execute('UPDATE project_chats SET excluded=1')
        self.proposal['projects'][0]['summary'] = '不应接受的变更'
        self.proposal['projects'][0]['items'][0]['title'] = '不应新增'
        self.apply()
        p = self.store.projects()[0]
        self.assertEqual('审批待联调', p['summary'])
        self.assertEqual(1, len(p['items']))

    def test_digest_restart_and_no_change_are_quiet(self):
        self.track()
        moment = datetime.now(CN).replace(hour=8, minute=0, second=0)
        make_notifications(self.store, DEFAULTS, moment)
        make_notifications(Store(self.root), DEFAULTS, moment)
        make_notifications(self.store, DEFAULTS, moment.replace(hour=12))
        self.assertEqual(1, len(self.store.rows('SELECT * FROM notifications')))

    def test_urgent_only_after_confirm_and_once(self):
        self.proposal['projects'][0]['items'][0]['priority'] = 'urgent'
        self.apply()
        moment = datetime.now(CN).replace(hour=0, minute=1)
        make_notifications(self.store, DEFAULTS, moment)
        self.assertEqual([], self.store.rows('SELECT * FROM notifications'))
        self.store.update_project(1, {'status': 'tracked'})
        moment = datetime.now(CN)
        make_notifications(self.store, DEFAULTS, moment)
        make_notifications(self.store, DEFAULTS, moment)
        self.assertEqual(1, len(self.store.rows("SELECT * FROM notifications WHERE dedup LIKE 'urgent:%'")))

    def test_old_import_is_not_urgent(self):
        for m in self.messages.values():
            m['ts'] -= 7 * 86400
        self.proposal['projects'][0]['items'][0]['priority'] = 'urgent'
        self.track()
        self.assertEqual(0, self.store.rows('SELECT sum(urgent) n FROM changes')[0]['n'])

    def test_deadline_without_new_messages_once_daily(self):
        self.track()
        moment = datetime.now(CN).replace(hour=8, minute=0, second=0)
        make_notifications(self.store, DEFAULTS, moment)
        with self.store.connect() as db:
            db.execute('UPDATE items SET due=?', ((moment+timedelta(hours=6)).isoformat(),))
        make_notifications(self.store, DEFAULTS, moment.replace(hour=12))
        make_notifications(self.store, DEFAULTS, moment.replace(hour=18))
        notes = self.store.rows('SELECT * FROM notifications')
        self.assertEqual(2, len(notes))
        self.assertIn('即将到期', notes[-1]['body'])

    def test_delivery_failure_is_retained_and_retry_succeeds(self):
        self.track()
        make_notifications(self.store, DEFAULTS, datetime.now(CN).replace(hour=12))
        def fail(*args):
            raise OSError('notification unavailable')
        deliver(self.store, DEFAULTS, fail)
        self.assertEqual(0, self.store.rows('SELECT delivered FROM notifications')[0]['delivered'])
        self.store.set('delivery', {})
        sent=[]
        deliver(self.store, DEFAULTS, lambda *args: sent.append(args))
        deliver(self.store, DEFAULTS, lambda *args: sent.append(args))
        self.assertEqual(1, len(sent))

    def test_source_same_second_pagination_delayed_insert_and_replay(self):
        path = self.root / 'source.db'
        chat = 'fake@chatroom'
        table = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute('INSERT INTO Name2Id VALUES (?)', ('fake_sender',))
            db.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content TEXT,WCDB_CT_message_content INTEGER)')
            db.executemany(f'INSERT INTO {table} VALUES (?,1,?,1,?,0)', [(i,self.now,f'正文 {i}') for i in range(1,1202)])
        source=WeChatSource.__new__(WeChatSource)
        source.account='fake'; source.shards=['message/message_0.db'];source.names={chat:'测试群','fake_sender':'测试人'}
        class Cache:
            def get(self, key): return str(path)
            def cleanup(self): pass
        source.cache=Cache()
        from wechat_cli.core.messages import decompress_content
        def fail_on_second_page(body, ct):
            return None if body == '正文 701' else decompress_content(body, ct)
        with patch('wechat_assistant.source.decompress_content', side_effect=fail_on_second_page):
            with self.assertRaises(RuntimeError):
                source.sync(self.store)
        self.assertEqual(503, self.store.status()['total'])
        self.assertEqual(701, source.sync(self.store)['new_or_changed'])
        self.assertEqual(0, source.sync(self.store)['new_or_changed'])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f'INSERT INTO {table} VALUES (1202,1,?,1,?,0)',(self.now-7*86400,'延迟补录'))
        self.assertEqual(1, source.sync(self.store)['new_or_changed'])
        self.assertEqual(1205, self.store.status()['total'])

    def test_config_cipher_decodes_only_verified_candidate(self):
        from wechat_cli.keys.config_cipher import MASK, NAME, scan_config_cipher
        import struct
        memory = bytearray(8192)
        base = 0x10000
        memory[64:64+len(NAME)] = NAME
        struct.pack_into('<QQ',memory,256,base+64,len(NAME))
        struct.pack_into('<Q',memory,280,base+512)
        key = bytes(range(32))
        plain = ("x'" + key.hex() + "'").encode()
        blob = bytes(b ^ MASK[i % len(MASK)] for i,b in enumerate(plain))
        struct.pack_into('<QQ',memory,512+0x90,base+1024,len(blob))
        memory[1024:1024+len(blob)] = blob
        def read(address, size):
            return bytes(memory[address-base:address-base+size]) if base <= address < base+len(memory) else None
        found, remaining = {}, {'salt'}
        with patch('wechat_cli.keys.config_cipher.verify_enc_key', side_effect=lambda k,p: k == key and p == b'page'):
            scan_config_cipher(read,[(base,len(memory))],[('db','path',4096,'salt',b'page')],found,remaining)
        self.assertEqual({'salt':key.hex()}, found)
        self.assertFalse(remaining)


if __name__ == '__main__':
    unittest.main()
