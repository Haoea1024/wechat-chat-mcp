import json
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def serve(service, port):
    token = secrets.token_urlsafe(24)
    static = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, value, status=200, content_type="application/json; charset=utf-8"):
            payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            route = urlparse(self.path)
            query = parse_qs(route.query)
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                return self.send({"error": "仅支持本机访问"}, 403)
            if route.path == "/":
                page = (static / "index.html").read_text("utf-8").replace("__TOKEN__", token)
                return self.send(page.encode(), content_type="text/html; charset=utf-8")
            if route.path == "/app.js":
                return self.send((static / "app.js").read_bytes(), content_type="text/javascript; charset=utf-8")
            if route.path == "/app.css":
                return self.send((static / "app.css").read_bytes(), content_type="text/css; charset=utf-8")
            if self.headers.get("X-Local-Token") != token:
                return self.send({"error": "请刷新本地页面"}, 403)
            try:
                if route.path == "/api/state":
                    return self.send(service.snapshot())
                if route.path == "/api/message":
                    from .retrieval import public_message
                    return self.send([public_message(service.store,m) for m in service.store.evidence([int(query["id"][0])])])
                if route.path == "/api/search":
                    from .retrieval import public_message
                    return self.send([public_message(service.store,m) for m in service.store.search(query.get("q", [""])[0], int(query["project"][0]) if query.get("project") else None)])
                if route.path == '/api/attachments':
                    from .retrieval import attachments
                    return self.send(attachments(service.store,query.get('project',[None])[0],int(query.get('offset',[0])[0])))
                if route.path == '/api/media':
                    from .media import MediaReader
                    rows=service.store.rows('SELECT * FROM media WHERE message_id=?',(int(query['id'][0]),))
                    return self.send(MediaReader.result(rows[0],int(query.get('start',[0])[0])) if rows else None)
                if route.path == '/api/media-file':
                    import mimetypes
                    rows=service.store.rows('SELECT path FROM media WHERE message_id=?',(int(query['id'][0]),))
                    if not rows or not rows[0]['path']:return self.send({'error':'附件尚未读取'},404)
                    file=Path(rows[0]['path'])
                    return self.send(file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                self.send({"error": "页面不存在"}, 404)
            except (ValueError, KeyError) as exc:
                self.send({"error": str(exc)}, 400)

        def do_POST(self):
            if self.headers.get("X-Local-Token") != token:
                return self.send({"error": "请刷新本地页面"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 1000000:
                    raise ValueError("请求过大")
                body = json.loads(self.rfile.read(size))
                if self.path == "/api/job":
                    return self.send(service.submit(body["action"], body))
                if self.path == "/api/settings":
                    service.configure(body)
                    return self.send({"ok": True})
                if self.path == "/api/stop":
                    self.send({"ok": True})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return
                if self.path == "/api/project":
                    service.store.update_project(int(body["id"]), body)
                    return self.send({"ok": True})
                if self.path == '/api/project-config':
                    from .work import configure_project
                    return self.send(configure_project(service.store,body['name'],body.get('aliases'),body.get('notes'),body.get('project_id'),body.get('status')))
                if self.path == '/api/feedback':
                    if body.get('revoke'):
                        service.store.revoke_feedback(int(body['revoke']))
                        return self.send({'ok':True})
                    fid=service.store.remember(body['text'],body.get('project_id'),body.get('chat_id'),body.get('message_id'))
                    return self.send({'feedback_id':fid})
                if self.path == "/api/item":
                    if body["state"] not in {"open", "done", "dismissed"}:
                        raise ValueError("事项状态无效")
                    with service.store.connect() as db:
                        db.execute("UPDATE items SET state=?,feedback_ids='[]' WHERE id=?", (body["state"], int(body["id"])))
                    service.store.set('context_dirty',True)
                    return self.send({"ok": True})
                if self.path == "/api/chat":
                    with service.store.connect() as db:
                        db.execute("INSERT OR REPLACE INTO project_chats VALUES (?,?,?)",
                                   (int(body["project_id"]), body["chat_id"], int(bool(body["excluded"]))))
                    service.store.set("context_dirty", True)
                    return self.send({"ok": True})
                self.send({"error": "接口不存在"}, 404)
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                self.send({"error": str(exc)}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    service.start()
    print(f"微信项目助理：http://127.0.0.1:{port} （关闭进程将停止采集和提醒）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop.set()
        service.thread.join(timeout=310)
        server.server_close()
