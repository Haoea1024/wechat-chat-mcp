import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

from wechat_assistant import web
from wechat_assistant.config import load, save
from wechat_assistant.service import Service
from wechat_assistant.work import configure_project
from wechat_assistant.web_runtime import ensure_web, probe, request, listening, legacy_command_matches


class WebUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            self.port = sock.getsockname()[1]
        _,config = load(self.root)
        config['port'] = self.port
        save(self.root,config)
        self.threads = []
        self.legacy_stopped = False

    def tearDown(self):
        if listening(self.port):
            current = probe(self.port)
            request(self.port,'/api/stop',current['token'],{})
        for thread in self.threads:thread.join(4)
        self.temp.cleanup()

    def wait(self):
        deadline=time.monotonic()+4
        while not listening(self.port):
            if time.monotonic()>deadline:raise AssertionError('test server did not start')
            time.sleep(.02)

    def start_current(self):
        service=Service(self.root,demo=True)
        configure_project(service.store,'升级测试项目')
        service.demo=False
        # No sync, model call, notifications, or user configuration access.
        def start():
            service.thread=threading.Thread(target=service.stop.wait,daemon=True)
            service.thread.start()
        service.start=start
        thread=threading.Thread(target=web.serve,args=(service,self.port),daemon=True)
        self.threads.append(thread);thread.start();self.wait()
        probe(self.port)  # Ensure the HTTP handler is ready before changing fixtures.
        return service

    def start_legacy(self):
        case=self
        class Legacy(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200 if self.path=='/' else 403);self.end_headers()
                self.wfile.write(b'<meta name="local-token" content="test-token"><link rel="stylesheet" href="/app.css">')
            def do_POST(self):
                if self.path=='/api/stop' and self.headers.get('X-Local-Token')=='test-token':
                    case.legacy_stopped=True
                    self.send_response(200);self.end_headers()
                    threading.Thread(target=server.shutdown,daemon=True).start()
                else:self.send_response(403);self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',self.port),Legacy)
        def run():
            try:server.serve_forever()
            finally:server.server_close()
        thread=threading.Thread(target=run,daemon=True)
        self.threads.append(thread);thread.start();self.wait()

    def spawn(self,*args,**kwargs):
        self.start_current()
        return SimpleNamespace(pid=12345,poll=lambda:None)

    def test_old_css_403_and_missing_schema_are_repaired_by_verified_restart(self):
        self.start_legacy()
        with self.assertRaises(urllib.error.HTTPError) as error:request(self.port,'/app.css')
        error.exception.close()
        with patch('wechat_assistant.web_runtime.legacy_owned',return_value=True),patch('wechat_assistant.web_runtime.subprocess.Popen',side_effect=self.spawn):
            result=ensure_web(self.root,only_if_running=True)
        self.assertEqual('restarted',result['web'])
        self.assertTrue(self.legacy_stopped)
        self.assertIn(b'.sidebar',request(self.port,'/app.css'))
        current=probe(self.port)
        self.assertEqual(str(self.root.resolve()),current['runtime']['data_dir'])
        state=json.loads(request(self.port,'/api/state',current['token']))
        self.assertEqual('client',state['settings']['provider'])
        self.assertIn('brief',state['projects'][0])

    def test_unknown_legacy_owner_is_not_stopped(self):
        self.start_legacy()
        with patch('wechat_assistant.web_runtime.legacy_owned',return_value=False),patch('wechat_assistant.web_runtime.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'无法确认'):ensure_web(self.root,only_if_running=True)
            spawn.assert_not_called()
        self.assertFalse(self.legacy_stopped)

    def test_current_instance_is_reused_and_wrong_data_directory_is_not_stopped(self):
        self.start_current()
        with patch('wechat_assistant.web_runtime.subprocess.Popen') as spawn:
            self.assertEqual('current',ensure_web(self.root)['web'])
            other=self.root/'other';_,config=load(other);config['port']=self.port;save(other,config)
            with self.assertRaisesRegex(ValueError,'其他资料目录'):ensure_web(other)
            spawn.assert_not_called()
        self.assertIsNotNone(probe(self.port))

    def test_changed_revision_is_restarted(self):
        with patch('wechat_assistant.web_runtime.fingerprint',return_value='old-revision'):
            self.start_current()
        with patch('wechat_assistant.web_runtime.subprocess.Popen',side_effect=self.spawn):
            self.assertEqual('restarted',ensure_web(self.root,only_if_running=True)['web'])

    def test_live_assets_do_not_change_under_old_python_process(self):
        package=self.root/'package';package.mkdir()
        shutil.copytree(Path(web.__file__).parent/'static',package/'static')
        with patch.object(web,'__file__',str(package/'web.py')):
            self.start_current()
        before=[request(self.port,path) for path in ('/','/app.js','/app.css')]
        for filename in ('index.html','app.js','app.css'):
            (package/'static'/filename).write_text('new-version','utf-8')
        after=[request(self.port,path) for path in ('/','/app.js','/app.css')]
        self.assertEqual(before,after)

    def test_refresh_does_not_start_an_unused_page(self):
        with patch('wechat_assistant.web_runtime.subprocess.Popen') as spawn:
            self.assertEqual('not_running',ensure_web(self.root,only_if_running=True)['web'])
            spawn.assert_not_called()

    def test_legacy_command_requires_explicit_matching_data_directory(self):
        root=self.root/'资料 with spaces'
        command=lambda args:subprocess.list2cmdline(args)
        self.assertTrue(legacy_command_matches(command(['python','-m','wechat_assistant.cli','--data-dir',str(root),'serve']),root))
        self.assertTrue(legacy_command_matches(command(['wechat-assistant.exe','--data-dir',str(root),'serve']),root))
        self.assertFalse(legacy_command_matches(command(['python','-m','wechat_assistant.cli','serve']),root))
        self.assertFalse(legacy_command_matches(command(['python','-m','other','--data-dir',str(root),'serve']),root))
        self.assertFalse(legacy_command_matches(command(['wechat-assistant.exe','--data-dir',str(root/'other'),'serve']),root))


if __name__=='__main__':unittest.main()
