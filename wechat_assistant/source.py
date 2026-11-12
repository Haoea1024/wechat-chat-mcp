"""Structured reads using the upstream wechat-cli decoder and database cache."""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from wechat_cli.core.config import load_config
from wechat_cli.core.db_cache import DBCache
from wechat_cli.core.key_utils import strip_key_metadata
from wechat_cli.core.messages import (
    _format_message_text, _load_name2id_maps, _resolve_sender_label,
    decompress_content, format_msg_type, find_msg_db_keys,
)


class WeChatSource:
    def __init__(self, root, config):
        cfg = load_config(config.get("wechat_config") or None)
        key_path = Path(cfg["keys_file"])
        if not key_path.exists():
            raise RuntimeError("尚未连接微信。请运行 wechat-assistant connect；保持电脑版微信已登录。")
        keys = strip_key_metadata(json.loads(key_path.read_text("utf-8")))
        if not keys:
            raise RuntimeError("微信密钥为空，尚未完成版本适配或登录。")
        self.db_dir = cfg["db_dir"]
        self.account = hashlib.sha256(str(Path(self.db_dir).resolve()).encode()).hexdigest()[:16]
        # Upstream's default cache only uses relative file names. Isolate accounts here.
        cache_dir = root / "wechat-cache" / self.account
        class AccountCache(DBCache):
            CACHE_DIR = str(cache_dir)
            MTIME_FILE = str(cache_dir / "mtimes.json")
        self.cache = AccountCache(keys, self.db_dir)
        self.shards = find_msg_db_keys(keys)
        present = {str(p.relative_to(self.db_dir)).replace("\\", "/")
                   for p in Path(self.db_dir).glob("message/*message_*.db")
                   if re.fullmatch(r"(?:biz_)?message_\d+\.db", p.name)}
        keyed = {s.replace("\\", "/") for s in self.shards}
        if present - keyed:
            raise RuntimeError(f"有 {len(present - keyed)} 个消息分库缺少密钥，请重新连接微信；未将本次采集标记为成功。")
        self.names = {}
        for rel, query in [
            ("contact/contact.db", "SELECT username,COALESCE(NULLIF(remark,''),NULLIF(nick_name,''),username) FROM contact"),
            ("session/session.db", "SELECT username,username FROM SessionTable"),
        ]:
            path = self.cache.get(rel)
            if not path:
                raise RuntimeError("无法读取联系人或会话数据库，请重新连接微信。")
            with closing(sqlite3.connect(path)) as db:
                for username, name in db.execute(query):
                    self.names.setdefault(username, name)
        username=Path(self.db_dir).parent.name.rsplit('_',1)[0]
        self.identity={'username':username,'display_name':self.names.get(username,'')} if username in self.names else {}

    def sync(self, store, days=30):
        if getattr(self,'identity',None):store.set('identity',self.identity)
        now = int(time.time())
        directory = self.sync_directory(store, now)
        floor = store.get("history_floor", now - days * 86400)
        store.set("history_floor", floor)
        changed, total_tables = 0, 0
        for shard in self.shards:
            path = self.cache.get(shard)
            if not path:
                raise RuntimeError("消息分库无法读取；保留之前的采集进度。")
            db = sqlite3.connect(path)
            try:
                senders = _load_name2id_maps(db)
                usernames = set(self.names) | set(senders.values())
                table_chats = {"Msg_" + hashlib.md5(u.encode()).hexdigest(): u for u in usernames}
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                          if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", r[0])]
                for table in tables:
                    chat = table_chats.get(table, "unknown:" + table)
                    if chat.startswith("gh_") or chat in {"newsapp", "brandsessionholder"}:
                        continue  # Official accounts are outside the agreed group/private scope.
                    name = self.names.get(chat, "未识别会话 " + table[-8:] if chat.startswith("unknown:") else chat)
                    source = self.account + "/" + shard.replace("\\", "/") + "/" + table
                    previous = store.rows("SELECT * FROM cursors WHERE source=?", (source,))
                    last_id = previous[0]["last_id"] if previous else 0
                    since = max(floor, previous[0]["last_sync"] - 2 * 86400) if previous else floor
                    # New local ids include delayed historical inserts. Overlap catches edits.
                    columns = {r[1] for r in db.execute(f'PRAGMA table_info([{table}])')}
                    server_column = 'server_id' if 'server_id' in columns else '0'
                    cursor = db.execute(f"""SELECT local_id,local_type,create_time,real_sender_id,
                        message_content,WCDB_CT_message_content,{server_column} FROM [{table}]
                        WHERE create_time>=? AND (local_id>? OR create_time>=?)
                        ORDER BY local_id""", (floor, last_id, since))
                    high_id = last_id
                    while rows := cursor.fetchmany(500):
                        records = []
                        for lid, kind, ts, sid, body, ct, server_id in rows:
                            raw = decompress_content(body, ct)
                            if raw is None:
                                raise RuntimeError("存在无法解码的消息，采集已暂停；未跳过该消息。")
                            raw = str(raw)
                            group = "@chatroom" in chat
                            sender_hint, text = _format_message_text(lid, kind, raw, group, chat, name,
                                                                    self.names, lambda u, n: n.get(u, u))
                            sender = _resolve_sender_label(sid, sender_hint, group, chat, name,
                                                           self.names, senders, lambda u, n: n.get(u, u))
                            uid = hashlib.sha256((source + "/" + str(lid)).encode()).hexdigest()
                            records.append(dict(uid=uid, chat_id=self.account + ":" + chat,
                                                chat_name=name, sender=sender or "未知发送者", ts=ts,
                                                kind=format_msg_type(kind), text=text or "", raw=raw,
                                                locator=json.dumps(dict(account=self.account,chat=chat,shard=shard,
                                                                        local_id=lid,server_id=server_id,time=ts,type=kind,
                                                                        sender_username=senders.get(sid,'')))))
                            high_id = max(high_id, lid)
                        # Keep the old scan time until all pages have succeeded.
                        changed += store.ingest(source, records, high_id, previous[0]["last_sync"] if previous else floor)
                    store.ingest(source, [], high_id, now)
                    total_tables += 1
            finally:
                db.close()
        self.cache.cleanup()
        result = {"ok": True, "at": now, "new_or_changed": changed, "conversations": total_tables,
                  "directory": directory,
                  "note": "已同步本机记录；图片、语音与文件可按消息读取，未下载内容会单独标明。"}
        store.set("sync", result)
        return result

    def sync_directory(self, store, now):
        # Same contact / chatroom_member relation used by upstream get_group_members.
        # Read identifiers directly, avoiding its process-global contact-name cache.
        members=[]
        status=dict(at=now,roster_available=False,self_username=getattr(self,'identity',{}).get('username',''))
        path=self.cache.get('contact/contact.db')
        if path:
            try:
                with closing(sqlite3.connect(path)) as db:
                    members=db.execute('''SELECT room.username,person.username FROM chatroom_member cm
                        JOIN contact room ON room.id=cm.room_id
                        JOIN contact person ON person.id=cm.member_id''').fetchall()
                status['roster_available']=True
            except sqlite3.Error as exc:
                status['note']='群成员表不可用；仍可通过已同步群消息中的发言人发现候选私聊。'
                status['error']=str(exc)
        else:
            status['note']='联系人数据库不可用；群成员线索仅来自已同步消息。'
        store.save_directory(self.account,self.names,members,status)
        return status
