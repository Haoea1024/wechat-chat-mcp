import hashlib
import time


def seed(store):
    now = int(time.time())
    texts = [
        ("demo:research@chatroom", "客户门户项目群（演示）", "项目负责人", "客户门户 的审批流程需要增加部门审核。请小周周五前确认角色权限。"),
        ("demo:zhou", "小周（演示）", "小周", "客户门户 的审批页已经做完，但部门审核还没接入。验收要等项目负责人确认。"),
        ("demo:research@chatroom", "客户门户项目群（演示）", "项目负责人", "周五演示只展示申请和审批流程，历史数据导入先不做。"),
        ("demo:life", "朋友（演示）", "朋友", "周末去吃火锅吗？"),
    ]
    rows = [dict(uid=hashlib.sha256(f"demo:{i}".encode()).hexdigest(), chat_id=chat, chat_name=name,
                 sender=sender, ts=now - 1800 + i * 120, kind="文本", text=text, raw=text)
            for i, (chat, name, sender, text) in enumerate(texts)]
    store.ingest("demo", rows, len(rows), now)
    store.set("sync", {"ok": True, "at": now, "note": "虚构演示数据，未连接真实微信。"})
