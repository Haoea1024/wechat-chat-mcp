import json
import sqlite3
import time
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, chat_id TEXT NOT NULL,
 chat_name TEXT NOT NULL, sender TEXT NOT NULL, ts INTEGER NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, raw TEXT NOT NULL, analyzed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_time ON messages(ts);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id,ts);
CREATE INDEX IF NOT EXISTS messages_pending ON messages(analyzed,id);
CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY,name TEXT,kind TEXT);
CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY,last_id INTEGER,last_sync INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,summary TEXT DEFAULT '',
 status TEXT DEFAULT 'candidate',notes TEXT DEFAULT '',paths TEXT DEFAULT '[]',
 evidence TEXT DEFAULT '[]',updated INTEGER);
CREATE TABLE IF NOT EXISTS project_chats (
 project_id INTEGER,chat_id TEXT,excluded INTEGER DEFAULT 0,PRIMARY KEY(project_id,chat_id));
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL,title TEXT NOT NULL,
 kind TEXT NOT NULL,owner TEXT DEFAULT '',due TEXT DEFAULT '',priority TEXT DEFAULT 'normal',
 reason TEXT DEFAULT '',state TEXT DEFAULT 'open',evidence TEXT DEFAULT '[]',updated INTEGER,
 UNIQUE(project_id,title));
CREATE TABLE IF NOT EXISTS changes (
 id INTEGER PRIMARY KEY,project_id INTEGER,item_id INTEGER,body TEXT,ts INTEGER,urgent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS notifications (
 id INTEGER PRIMARY KEY,dedup TEXT UNIQUE,title TEXT,body TEXT,created INTEGER,
 delivered INTEGER DEFAULT 0,error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS feedback (
 id INTEGER PRIMARY KEY,project_id INTEGER,chat_id TEXT,message_id INTEGER,
 text TEXT NOT NULL,active INTEGER DEFAULT 1,created INTEGER);
CREATE TABLE IF NOT EXISTS media (
 message_id INTEGER PRIMARY KEY,status TEXT,kind TEXT,path TEXT DEFAULT '',
 text TEXT DEFAULT '',detail TEXT DEFAULT '{}',updated INTEGER);
"""


class Store:
    def __init__(self, root):
        self.path = root / "assistant.db"
        with self.connect() as db:
            db.executescript(SCHEMA)
            if 'overview' not in {row[1] for row in db.execute('PRAGMA table_info(projects)')}:
                db.execute("ALTER TABLE projects ADD COLUMN overview TEXT DEFAULT '{}'")
            if 'locator' not in {row[1] for row in db.execute('PRAGMA table_info(messages)')}:
                db.execute("ALTER TABLE messages ADD COLUMN locator TEXT DEFAULT '{}'")
            if 'feedback_ids' not in {row[1] for row in db.execute('PRAGMA table_info(items)')}:
                db.execute("ALTER TABLE items ADD COLUMN feedback_ids TEXT DEFAULT '[]'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def get(self, key, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def ingest(self, source, records, last_id, now):
        """Messages and their cursor commit together; replay is harmless."""
        changed = 0
        with self.connect() as db:
            for m in records:
                old = db.execute("SELECT text,raw FROM messages WHERE uid=?", (m["uid"],)).fetchone()
                if old and old["text"] == m["text"] and old["raw"] == m["raw"]:
                    if 'locator' in m:
                        db.execute('UPDATE messages SET locator=? WHERE uid=?', (m['locator'],m['uid']))
                    continue
                db.execute("""INSERT INTO messages(uid,chat_id,chat_name,sender,ts,kind,text,raw)
                    VALUES (:uid,:chat_id,:chat_name,:sender,:ts,:kind,:text,:raw)
                    ON CONFLICT(uid) DO UPDATE SET text=excluded.text,raw=excluded.raw,
                    sender=excluded.sender,chat_name=excluded.chat_name,analyzed=0""", m)
                if 'locator' in m:
                    db.execute('UPDATE messages SET locator=? WHERE uid=?', (m['locator'],m['uid']))
                db.execute("INSERT OR REPLACE INTO chats VALUES (?,?,?)",
                           (m["chat_id"], m["chat_name"], "group" if "@chatroom" in m["chat_id"] else "private"))
                changed += 1
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (source, last_id, now))
        return changed

    def projects(self, include_archived=False):
        projects = self.rows("SELECT * FROM projects ORDER BY updated DESC,id")
        fixed = self.get('work_categories')
        if fixed is not None and not include_archived:
            ids = {p['id'] for p in fixed}
            projects = [p for p in projects if p['id'] in ids]
        for p in projects:
            p["paths"] = json.loads(p["paths"])
            p["evidence"] = json.loads(p["evidence"])
            p["overview"] = json.loads(p["overview"])
            p['feedback'] = self.feedback(p['id'])
            p['fixed'] = any(c['id']==p['id'] for c in (fixed or []))
            p['aliases'] = next((c.get('aliases',[]) for c in (fixed or []) if c['id']==p['id']),[])
            p["chats"] = self.rows("""SELECT pc.chat_id,pc.excluded,c.name FROM project_chats pc
                LEFT JOIN chats c ON c.id=pc.chat_id WHERE pc.project_id=?""", (p["id"],))
            p["items"] = self.rows("SELECT * FROM items WHERE project_id=? ORDER BY updated DESC", (p["id"],))
            for item in p["items"]:
                item["evidence"] = json.loads(item["evidence"])
                item['feedback_ids']=json.loads(item['feedback_ids'])
        return projects

    def feedback(self, project_id=None):
        if project_id is None:
            return self.rows('SELECT * FROM feedback WHERE active=1 ORDER BY id')
        return self.rows('SELECT * FROM feedback WHERE active=1 AND (project_id=? OR project_id IS NULL) ORDER BY id', (project_id,))

    def remember(self, text, project_id=None, chat_id=None, message_id=None):
        if not text.strip():
            raise ValueError('请填写反馈内容。')
        with self.connect() as db:
            if project_id and not db.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():
                raise ValueError('找不到项目。')
            fid=db.execute('INSERT INTO feedback(project_id,chat_id,message_id,text,created) VALUES (?,?,?,?,?)',
                           (project_id,chat_id,message_id,text.strip(),int(time.time()))).lastrowid
        self.set('context_dirty',True)
        self.set('feedback_dirty',True)
        return fid

    def revoke_feedback(self, fid):
        with self.connect() as db:
            db.execute('UPDATE feedback SET active=0 WHERE id=?',(fid,))
        self.set('context_dirty',True)
        self.set('feedback_dirty',True)
        with self.connect() as db:
            for row in db.execute("SELECT id,feedback_ids FROM items WHERE feedback_ids!='[]'").fetchall():
                if fid in json.loads(row['feedback_ids']):
                    db.execute("UPDATE items SET state='open',feedback_ids='[]' WHERE id=?",(row['id'],))

    def update_project(self, pid, values):
        allowed = {k: v for k, v in values.items() if k in {"name", "status", "notes", "paths"}}
        if "status" in allowed and allowed["status"] not in {"candidate", "tracked", "ignored"}:
            raise ValueError("项目状态无效")
        if "paths" in allowed:
            allowed["paths"] = json.dumps(allowed["paths"], ensure_ascii=False)
        if not allowed:
            return
        with self.connect() as db:
            old = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if not old:
                raise ValueError("找不到项目")
            db.execute("UPDATE projects SET " + ",".join(k + "=?" for k in allowed) + ",updated=? WHERE id=?",
                       (*allowed.values(), int(time.time()), pid))
            if "notes" in allowed or "paths" in allowed or "status" in allowed:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("context_dirty", "true"))
            if allowed.get("status") == "tracked" and old["status"] != "tracked":
                db.execute("INSERT INTO changes(project_id,body,ts) VALUES (?,?,?)",
                           (pid, "开始跟踪：" + old["name"], int(time.time())))
        if self.get('work_categories') is not None:
            from .work import persist_projects
            persist_projects(self)

    def search(self, query, project_id=None, limit=60):
        params = ["%" + query + "%"]
        sql = "SELECT * FROM messages WHERE text LIKE ?"
        if project_id:
            sql += " AND chat_id IN (SELECT chat_id FROM project_chats WHERE project_id=? AND excluded=0)"
            params.append(project_id)
        return self.rows(sql + " ORDER BY ts DESC,id DESC LIMIT ?", (*params, limit))

    def evidence(self, ids):
        if not ids:
            return []
        return self.rows("SELECT * FROM messages WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY ts,id", ids)

    def status(self):
        count = self.rows("SELECT count(*) total,sum(analyzed=0) pending,min(ts) earliest,max(ts) latest FROM messages")[0]
        return count | {"sync": self.get("sync"), "analysis": self.get("analysis"),
                        "worker": self.get("worker"), "delivery": self.get("delivery")}
