import queue
import threading
import time
import json

from .analysis import analyze, ask
from .config import load, save
from .model import Model
from .notify import make_notifications, deliver
from .source import WeChatSource
from .store import Store


class Service:
    def __init__(self, root=None, demo=False):
        self.root, self.config = load(root)
        self.store = Store(self.root)
        self.demo = demo
        if not demo:
            from .work import initialize_work
            initialize_work(self.store)
            _, self.config = load(self.root)
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.Lock()

    def settings(self):
        return self.config.copy()

    def configure(self, values):
        permitted = {"provider", "model", "api_base", "api_key_env", "codex_command", "wechat_config",
                     "history_days", "poll_seconds", "analysis_seconds", "batch_size", "schedule", "notifications", "background_analysis"}
        _, saved = load(self.root)
        updated = saved | {k: v for k, v in values.items() if k in permitted}
        if updated["provider"] not in {"client", "codex", "api"}:
            raise ValueError("模型方式必须为 client、codex 或 api。")
        if updated['provider']=='client':updated['background_analysis']=False
        for key, low, high in [("history_days", 1, 3650), ("poll_seconds", 15, 3600),
                               ("analysis_seconds", 30, 86400), ("batch_size", 10, 500)]:
            if not isinstance(updated[key], int) or not low <= updated[key] <= high:
                raise ValueError(f"{key} 范围应为 {low}–{high}")
        from datetime import datetime
        if not updated["schedule"]:
            raise ValueError("请至少保留一个简报时间。")
        for slot in updated["schedule"]:
            datetime.strptime(slot, "%H:%M")
        self.config = updated
        save(self.root, updated)

    def submit(self, action, payload=None):
        if action not in {"sync", "analyze", "ask", "backfill", "notify_test", "read_media"}:
            raise ValueError("未知任务")
        if not self.demo:_, self.config = load(self.root)
        if action in {'analyze','ask'} and self.config['provider']=='client':
            return self.client_handoff(payload or {})
        if self.jobs.qsize() >= 3:
            raise ValueError("已有任务等待处理，请稍后再试。")
        self.jobs.put((action, payload or {}))
        return {"queued": True}

    def client_handoff(self,payload):
        projects=self.store.projects()
        selected=[p['name'] for p in projects if p['id']==payload.get('project_id')]
        request=payload.get('question') or ('请更新项目“'+selected[0]+'”的进展。' if selected else '请更新我已配置的项目资料。')
        return {'handoff': True, 'prompt': request+' 先查看状态和已有项目资料，按需读取新增聊天及附件。明确指定项目的查询默认在最终回答前用 save_project_brief 保存新增结论、修订及实际已读范围，无需再询问；保留出处和未读范围，成功后网页即可查看。用户要求不保存或资料与覆盖均无变化时跳过。不要全量分析所有聊天。',
                'note': '复制到当前 WorkBuddy / Codex 对话执行。网页不会启动其他模型；保存后此页自动显示资料。'}

    def execute(self, action, payload):
        if not self.demo:_, self.config = load(self.root)
        if action in {'analyze','ask'} and self.config['provider']=='client':
            return self.client_handoff(payload)
        if action == "sync":
            if self.demo:
                return {"note": "演示模式不访问真实微信。"}
            return WeChatSource(self.root, self.config).sync(self.store, self.config["history_days"])
        if action == "analyze":
            return analyze(self.store, Model(self.root, self.config), self.config)
        if action == "ask":
            return ask(self.store, Model(self.root, self.config), payload["question"], payload.get("project_id"))
        if action == 'read_media':
            from .media import MediaReader
            return MediaReader(self.root,self.config,self.store).read(int(payload['message_id']),int(payload.get('start',0)),bool(payload.get('force',False)),payload.get('member'))
        if action == "backfill":
            days = int(payload["days"])
            if not 1 <= days <= 3650:
                raise ValueError("回溯天数应为 1–3650。")
            self.store.set("history_floor", min(self.store.get("history_floor", int(time.time())), int(time.time()) - days * 86400))
            with self.store.connect() as db:
                db.execute("DELETE FROM cursors")
            return self.execute("sync", {})
        if action == "notify_test":
            self.store.set("delivery", {})
            with self.store.connect() as db:
                db.execute("INSERT INTO notifications(dedup,title,body,created) VALUES (?,?,?,?)",
                           ("test:" + str(time.time_ns()), "微信项目助理", "测试提醒。完整项目简报保存在本地页面。", int(time.time())))
            deliver(self.store, self.config)
            return self.store.get("delivery")
        raise ValueError("未知任务")

    def perform(self, action, payload=None):
        with self.lock:
            self.store.set("worker", {"action": action, "running": True, "at": int(time.time())})
            try:
                result = self.execute(action, payload or {})
                self.store.set("job", {"action": action, "ok": True, "at": int(time.time()), "result": result})
                if action in {'ask','read_media'} and not result.get('handoff'):
                    self.store.set(action+'_result',{'at':time.time_ns(),'result':result})
                return result
            except Exception as exc:
                error = {"ok": False, "at": int(time.time()), "error": str(exc)}
                self.store.set("job", {"action": action} | error)
                if action in {"sync", "analyze"}:
                    self.store.set("sync" if action == "sync" else "analysis", error)
                    with self.store.connect() as db:
                        db.execute("INSERT OR IGNORE INTO notifications(dedup,title,body,created) VALUES (?,?,?,?)",
                                   (f"failure:{action}:{time.strftime('%Y-%m-%d')}", "项目助理运行需要检查",
                                    ("采集" if action == "sync" else "模型分析") + "失败：" + str(exc), int(time.time())))
                raise
            finally:
                self.store.set("worker", {"action": action, "running": False, "at": int(time.time())})

    def run(self):
        next_sync, next_analysis = 0, 0
        while not self.stop.is_set():
            if not self.demo:_, self.config = load(self.root)
            try:
                action, payload = self.jobs.get(timeout=1)
            except queue.Empty:
                now = time.time()
                action, payload = None, {}
                if not self.demo and now >= next_sync:
                    action, next_sync = "sync", now + self.config["poll_seconds"]
                elif self.config['provider']!='client' and self.config.get('background_analysis',False) and self.store.projects() and now >= next_analysis and (self.store.status()["pending"] or self.store.get("context_dirty") or self.store.get('overview_refresh')
                                               or any(p['paths'] for p in self.store.projects() if p['status'] == 'tracked')):
                    action, next_analysis = "analyze", now + self.config["analysis_seconds"]
            if action:
                try:
                    self.perform(action, payload)
                    if action == "analyze" and self.store.status()["pending"]:
                        next_analysis = time.time() + 30  # Drain initial history in batches.
                except Exception:
                    pass  # Detailed failure is already visible in local status; the next run retries.
            try:
                make_notifications(self.store, self.config)
                deliver(self.store, self.config)
            except Exception as exc:
                self.store.set("delivery", {"ok": False, "at": int(time.time()), "error": str(exc)})

    def start(self):
        self.thread = threading.Thread(target=self.run, name="wechat-assistant-worker", daemon=True)
        self.thread.start()

    def snapshot(self):
        if not self.demo:_, self.config = load(self.root)
        projects=self.store.projects()
        for project in projects:
            rows=self.store.rows('SELECT body,version,created FROM project_briefs WHERE project_id=? ORDER BY version DESC LIMIT 1',(project['id'],))
            project['brief']=dict(json.loads(rows[0]['body']),version=rows[0]['version'],created=rows[0]['created']) if rows else None
        return {"status": self.store.status(), "projects": projects,
                "chats": self.store.rows("SELECT * FROM chats ORDER BY name"),
                "notifications": self.store.rows("SELECT * FROM notifications ORDER BY id DESC LIMIT 40"),
                "job": self.store.get("job"), "answer":self.store.get('ask_result'),
                "media_result":self.store.get('read_media_result'), "feedback":self.store.feedback(),
                "settings": self.settings(), "demo": self.demo}
