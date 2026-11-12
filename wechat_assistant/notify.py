import json
import os
import subprocess
import time
from datetime import datetime, timedelta

from .analysis import CN


def make_notifications(store, config, now=None):
    now = now or datetime.now(CN)
    with store.connect() as db:
        # Urgent notifications only come from confirmed, tracked projects.
        for row in db.execute("""SELECT c.*,p.name FROM changes c JOIN projects p ON p.id=c.project_id
            WHERE c.urgent=1 AND p.status='tracked' AND c.ts>?""", (int(now.timestamp()) - 6 * 3600,)).fetchall():
            db.execute("INSERT OR IGNORE INTO notifications(dedup,title,body,created) VALUES (?,?,?,?)",
                       ("urgent:" + str(row["id"]), "需及时处理 · " + row["name"], row["body"], int(now.timestamp())))
        slots = [now.replace(hour=int(t.split(":")[0]), minute=int(t.split(":")[1]), second=0, microsecond=0)
                 for t in config["schedule"]]
        elapsed = [t for t in slots if t <= now]
        if not elapsed:
            return
        slot = max(elapsed).isoformat()
        previous = db.execute("SELECT value FROM settings WHERE key='last_digest_slot'").fetchone()
        if previous and json.loads(previous[0]) == slot:
            return
        checkpoint = db.execute("SELECT value FROM settings WHERE key='digest_change_id'").fetchone()
        last_id = json.loads(checkpoint[0]) if checkpoint else 0
        changes = db.execute("""SELECT c.*,p.name,p.status FROM changes c JOIN projects p ON p.id=c.project_id
            WHERE c.id>? AND p.status!='ignored' ORDER BY c.id""", (last_id,)).fetchall()
        lines = []
        seen_items = set()
        for change in reversed(changes):
            if change["status"] == "candidate":
                label, identity = "待确认项目：" + change["name"], ("candidate", change["project_id"])
            else:
                label, identity = change["name"] + "：" + change["body"], ("item", change["item_id"] or -change["id"])
            if identity not in seen_items:
                seen_items.add(identity)
                lines.append(label)
        # Deadlines can become actionable without a new chat message.
        for item in db.execute("""SELECT i.*,p.name FROM items i JOIN projects p ON p.id=i.project_id
            WHERE p.status='tracked' AND i.state='open' AND i.due!=''""").fetchall():
            due = datetime.fromisoformat(item["due"])
            due = due.replace(tzinfo=CN) if due.tzinfo is None else due.astimezone(CN)
            if len(item["due"]) == 10:
                due = due.replace(hour=23, minute=59)
            key = f"due:{item['id']}:{item['due']}:{now.date()}"
            already = db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
            if due <= now + timedelta(hours=24) and not already:
                lines.append(f"{'已逾期' if due < now else '即将到期'} · {item['name']}：{item['title']}（{item['due']}）")
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, "true"))
        if lines:
            db.execute("INSERT OR IGNORE INTO notifications(dedup,title,body,created) VALUES (?,?,?,?)",
                       ("digest:" + slot, "项目简报 · " + max(elapsed).strftime("%m/%d %H:%M"),
                        "\n\n".join(reversed(lines)), int(now.timestamp())))
        highest = db.execute("SELECT COALESCE(MAX(id),0) FROM changes").fetchone()[0]
        db.execute("INSERT OR REPLACE INTO settings VALUES ('digest_change_id',?)", (json.dumps(highest),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('last_digest_slot',?)", (json.dumps(slot),))


def windows_toast(title, body):
    # Pass text as JSON on stdin, never interpolate chat text into PowerShell code.
    script = r'''
$ErrorActionPreference = 'Stop'
$registration = 'HKCU:\Software\Classes\AppUserModelId\WeChatProjectAssistant'
New-Item -Path $registration -Force > $null
New-ItemProperty -Path $registration -Name DisplayName -Value 'WeChat Project Assistant' -PropertyType String -Force > $null
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($payload.title)) > $null
$nodes.Item(1).AppendChild($xml.CreateTextNode($payload.body)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('WeChatProjectAssistant').Show($toast)
'''
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            input=json.dumps({"title": title, "body": body[:350]}, ensure_ascii=True),
                            capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Windows 通知提交失败，请检查通知权限；完整简报仍保存在本地页面。")


def deliver(store, config, sender=None):
    if not config["notifications"]:
        return
    previous = store.get("delivery", {})
    if previous.get("ok") is False and time.time() - previous.get("at", 0) < 300:
        return
    if sender is None and os.name != "nt":
        return  # Local inbox remains available on non-Windows systems.
    sender = sender or windows_toast
    for n in store.rows("SELECT * FROM notifications WHERE delivered=0 ORDER BY id LIMIT 10"):
        try:
            sender(n["title"], n["body"])
        except Exception as exc:
            store.set("delivery", {"ok": False, "at": int(time.time()), "error": str(exc)})
            with store.connect() as db:
                db.execute("UPDATE notifications SET error=? WHERE id=?", (str(exc), n["id"]))
            break
        else:
            with store.connect() as db:
                db.execute("UPDATE notifications SET delivered=1,error='' WHERE id=?", (n["id"],))
            store.set("delivery", {"ok": True, "at": int(time.time()), "note": "已提交系统通知；阅读状态未知。"})
