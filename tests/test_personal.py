import copy
import hashlib
import io
import json
import sqlite3
import struct
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from wechat_assistant.analysis import apply_analysis
from wechat_assistant.documents import read_document
from wechat_assistant.images import decode_dat
from wechat_assistant.media import MediaReader
from wechat_assistant.retrieval import conversation, project_context
from wechat_assistant.store import Store
from wechat_assistant.work import initialize_work,configure_project


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root);self.now=int(time.time())
        initialize_work(self.store)
        configure_project(self.store,'测试物流项目')
        self.pid=configure_project(self.store,'测试订单项目',aliases=['订单同步'])['id']
        records=[dict(uid=str(i),chat_id='test:group' if i<3 else 'test:person',chat_name='测试会话',
                      sender='测试人员',ts=self.now+i,kind='文本',text='测试订单项目 待联调',raw='测试原文',locator='{}') for i in range(6)]
        self.store.ingest('test',records,6,self.now)
        self.messages={m['id']:m for m in self.store.evidence(list(range(1,7)))}

    def tearDown(self):self.temp.cleanup()

    def proposal(self):
        return {'projects':[dict(id=self.pid,name='测试订单项目',summary='尚待联调',evidence=[1],feedback_ids=[],
            overview=dict(next_steps=['联调'],uncertainties=[],conversations=[dict(summary='准备联调',evidence=[1])]),
            items=[dict(id=0,title='接口联调',kind='task',owner='',due='',priority='normal',reason='测试',state='open',evidence=[1],feedback_ids=[])])]}

    def test_fixed_categories_reject_new_projects(self):
        proposal=self.proposal();proposal['projects'][0]['id']=0;proposal['projects'][0]['name']='闲聊话题'
        with self.assertRaises(ValueError):apply_analysis(self.store,proposal,self.messages,[1])
        self.assertEqual(2,len(self.store.projects()))
        self.assertEqual(6,self.store.status()['pending'])

    def test_fresh_install_is_empty_and_background_analysis_is_opt_in(self):
        from wechat_assistant.service import Service
        root=self.root/'fresh';root.mkdir()
        service=Service(root)
        self.assertEqual([],service.store.projects())
        self.assertEqual([],service.store.feedback())
        self.assertFalse(service.config['background_analysis'])

    def test_project_configuration_survives_restart_and_preserves_manual_state(self):
        apply_analysis(self.store,self.proposal(),self.messages,[])
        with self.store.connect() as db:db.execute("UPDATE items SET state='done'")
        fid=self.store.remember('测试补充',self.pid)
        changed=configure_project(self.store,'新版测试订单',aliases=['同步服务'],project_id=self.pid)
        self.assertEqual(self.pid,changed['id'])
        initialize_work(Store(self.root))
        self.assertEqual('done',self.store.rows('SELECT state FROM items')[0]['state'])
        self.assertIn(fid,[f['id'] for f in self.store.feedback()])
        from wechat_assistant.work import resolve_project
        self.assertEqual(self.pid,resolve_project(self.store,'同步服务')['id'])
        self.store.update_project(self.pid,{'notes':'页面补充说明'})
        initialize_work(Store(self.root))
        self.assertEqual('页面补充说明',resolve_project(self.store,str(self.pid))['notes'])

    def test_removing_project_from_local_config_archives_without_deleting_evidence(self):
        from wechat_assistant.config import load,save
        root,cfg=load(self.root)
        cfg['projects']=[p for p in cfg['projects'] if p['id']!=self.pid];save(root,cfg)
        initialize_work(self.store)
        self.assertNotIn(self.pid,[p['id'] for p in self.store.projects()])
        self.assertEqual(6,self.store.status()['total'])
        self.assertEqual('ignored',self.store.rows('SELECT status FROM projects WHERE id=?',(self.pid,))[0]['status'])

    def test_account_selection_does_not_guess_when_multiple_candidates(self):
        from wechat_cli.core.config import _choose_candidate
        with patch('sys.stdin.isatty',return_value=False):
            with self.assertRaises(ValueError):_choose_candidate(['account-a','account-b'])

    def test_feedback_can_confirm_completion_and_be_revoked(self):
        apply_analysis(self.store,self.proposal(),self.messages,[])
        fid=self.store.remember('接口联调已经完成',self.pid)
        p=self.proposal();item=p['projects'][0]['items'][0];item.update(state='done',feedback_ids=[fid],evidence=[])
        apply_analysis(self.store,p,self.messages,[])
        self.assertEqual('done',self.store.rows('SELECT state FROM items')[0]['state'])
        self.store.revoke_feedback(fid)
        self.assertEqual('open',self.store.rows('SELECT state FROM items')[0]['state'])
        with self.assertRaises(ValueError):apply_analysis(self.store,p,self.messages,[])

    def test_feedback_from_other_work_cannot_confirm(self):
        fid=self.store.remember('已经完成',self.store.get('work_categories')[0]['id'])
        p=self.proposal();p['projects'][0]['items'][0].update(state='done',feedback_ids=[fid])
        with self.assertRaises(ValueError):apply_analysis(self.store,p,self.messages,[])

    def test_conversation_identity_comes_from_evidence(self):
        p=self.proposal();apply_analysis(self.store,p,self.messages,[])
        project=next(p for p in self.store.projects() if p['id']==self.pid)
        self.assertEqual('test:group',project['overview']['conversations'][0]['chat_id'])
        bad=self.proposal();bad['projects'][0]['overview']['conversations'][0]['evidence']=[1,4]
        with self.assertRaises(ValueError):apply_analysis(self.store,bad,self.messages,[])

    def test_pagination_covers_same_second_and_both_chats(self):
        first=project_context(self.store,str(self.pid),limit=2)
        self.assertEqual(6,first['retrieval']['total_matches']);self.assertEqual(2,first['retrieval']['next_offset'])
        ids=[];before=None
        while True:
            result=conversation(self.store,'test:group',before,limit=2);ids.extend(m['id'] for m in result['messages']);before=result['next_before_id']
            if before is None:break
        self.assertEqual({1,2,3},set(ids));self.assertEqual(3,len(ids))

    def test_locator_replay_does_not_reopen_messages(self):
        with self.store.connect() as db:db.execute('UPDATE messages SET analyzed=1')
        record=dict(self.messages[1],locator=json.dumps({'local_id':99}))
        self.store.ingest('test',[record],6,self.now)
        self.assertEqual(0,self.store.status()['pending'])
        self.assertEqual(99,json.loads(self.store.evidence([1])[0]['locator'])['local_id'])

    def test_image_aes_and_xor_roundtrip(self):
        key=b'0123456789abcdef';prefix=b'\x89PNG test prefix';middle=b'untouched';tail=b'tail-data'
        encrypted=AES.new(key,AES.MODE_ECB).encrypt(pad(prefix,16))
        dat=bytes.fromhex('070856320807')+struct.pack('<II',len(prefix),len(tail))+b'\0'+encrypted+middle+bytes(x^71 for x in tail)
        self.assertEqual(prefix+middle+tail,decode_dat(dat,key,71))

    def test_document_paging_and_embedded_images(self):
        from docx import Document
        from PIL import Image
        from pptx import Presentation
        from pptx.util import Inches
        image=self.root/'figure.png';Image.new('RGB',(20,20),'red').save(image)
        doc=Document();doc.add_paragraph('测试需求');doc.add_table(rows=1,cols=1).cell(0,0).text='表格数据';doc.add_picture(str(image));doc.save(self.root/'test.docx')
        vision=lambda path:'图片中的测试内容'
        result=read_document(self.root/'test.docx',self.root/'output',vision)
        self.assertIn('测试需求',result['parts'][0]['text']);self.assertIn('图片中的测试内容',result['parts'][1]['text'])
        deck=Presentation()
        for i in range(7):
            slide=deck.slides.add_slide(deck.slide_layouts[6]);slide.shapes.add_textbox(0,0,Inches(3),Inches(1)).text=f'Slide {i}'
        deck.save(self.root/'test.pptx')
        first=read_document(self.root/'test.pptx',self.root/'output',vision);last=read_document(self.root/'test.pptx',self.root/'output',vision,first['next_start'])
        self.assertEqual(7,len(first['parts'])+len(last['parts']));self.assertIsNone(last['next_start'])

    def test_scanned_pdf_and_sheet_formulas(self):
        from PIL import Image
        from openpyxl import Workbook
        Image.new('RGB',(100,100),'white').save(self.root/'scan.pdf')
        result=read_document(self.root/'scan.pdf',self.root/'output',lambda p:'扫描页识别文字')
        self.assertIn('扫描页识别文字',result['parts'][0]['text'])
        book=Workbook();sheet=book.active;sheet.append(['需求','数量']);sheet.append(['审批',3]);sheet.append(['合计','=SUM(B2:B2)']);book.save(self.root/'test.xlsx')
        result=read_document(self.root/'test.xlsx',self.root/'output',lambda p:'')
        self.assertIn('=SUM(B2:B2)',result['parts'][0]['text'])

    def test_missing_attachment_is_not_empty_success(self):
        with self.store.connect() as db:db.execute('UPDATE messages SET locator=?,kind=? WHERE id=1',(json.dumps(dict(account='test',type=3)),'图片'))
        reader=MediaReader(self.root,{},self.store);reader._source=SimpleNamespace(account='test')
        with patch.object(reader,'_image_path',side_effect=FileNotFoundError('图片已清理')):
            result=reader.read(1)
        self.assertEqual('missing',result['status']);self.assertIn('已清理',result['detail']['error'])

    def test_file_lookup_uses_content_not_duplicate_name(self):
        folder=self.root/'account'/'msg'/'file'/'2026-11';folder.mkdir(parents=True)
        old=folder/'proposal.txt';old.write_text('旧版本','utf-8')
        new=folder/'proposal(1).txt';new.write_text('新版本','utf-8')
        digest=hashlib.md5(new.read_bytes()).hexdigest()
        index=self.root/'hardlink.db'
        with closing(sqlite3.connect(index)) as db:
            db.executescript('CREATE TABLE dir2id(username);INSERT INTO dir2id VALUES ("2026-11");CREATE TABLE file_hardlink_info_v4(md5,file_name,dir1,dir2);')
            db.execute('INSERT INTO file_hardlink_info_v4 VALUES (?,?,1,0)',(hashlib.md5(old.read_bytes()).hexdigest(),old.name))
            db.execute('INSERT INTO file_hardlink_info_v4 VALUES (?,?,1,0)',(digest,new.name))
            db.commit()
        reader=MediaReader(self.root,{},self.store)
        reader._source=SimpleNamespace(db_dir=str(self.root/'account'/'db_storage'),cache=SimpleNamespace(get=lambda _:str(index)))
        message={'raw':f'<msg><appmsg><title>proposal.txt</title><md5>{digest}</md5></appmsg></msg>'}
        with patch.object(reader,'_resources',return_value=None):
            self.assertEqual(new,reader._file_path(message,{}))
            message['raw']='<msg><appmsg><title>proposal.txt</title></appmsg></msg>'
            with self.assertRaises(FileNotFoundError):reader._file_path(message,{})

    def test_zip_member_content_is_cached_separately_from_listing(self):
        archive=self.root/'files.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('note.txt','实际正文')
        with self.store.connect() as db:db.execute('UPDATE messages SET locator=?,kind=? WHERE id=1',(json.dumps(dict(account='test',type=49)),'链接/文件'))
        reader=MediaReader(self.root,{},self.store);reader._source=SimpleNamespace(account='test')
        with patch.object(reader,'_file_path',return_value=archive):
            listed=reader.read(1);member=reader.read(1,member='note.txt')
        self.assertEqual('listed',listed['status']);self.assertEqual('实际正文',member['text'])
        self.assertIn('实际正文',self.store.rows('SELECT text FROM media')[0]['text'])


if __name__=='__main__':unittest.main()
