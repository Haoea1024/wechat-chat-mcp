import hashlib
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import ANSWER_SCHEMA

CN = timezone(timedelta(hours=8))


def project_context(project):
    """Only explicitly attached files and short Git/README facts enter the prompt."""
    pieces = []
    for value in project["paths"]:
        path = Path(value).expanduser()
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".rst", ".py", ".ts", ".js"}:
            pieces.append({"source": str(path), "text": path.read_text("utf-8", errors="replace")[:12000]})
        elif path.is_dir():
            for name in ("README.md", "TODO.md", "STATUS.md"):
                file = path / name
                if file.exists():
                    pieces.append({"source": str(file), "text": file.read_text("utf-8", errors="replace")[:6000]})
            if (path / ".git").exists():
                for args in (["status", "--short"], ["log", "-6", "--date=iso", "--format=%h %ad %s", "--stat"]):
                    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                                            text=True, encoding="utf-8", errors="replace", timeout=15)
                    pieces.append({"source": str(path) + " git " + args[0], "text": result.stdout[:6000]})
        else:
            pieces.append({"source": str(path), "text": "路径不存在或不支持此文件类型，不能据此判断进度。"})
    return pieces


def compact_message(m, store=None):
    result = {"id": m["id"], "chat_id": m["chat_id"], "chat": m["chat_name"], "sender": m["sender"],
            "time": datetime.fromtimestamp(m["ts"], CN).isoformat(), "kind": m["kind"],
            "text": m["text"][:3500], "truncated": len(m["text"]) > 3500}
    if store:
        from .retrieval import public_message
        public=public_message(store,m)
        result['attachment']=public.get('attachment')
        result['is_self']=public['is_self']
    return result


def analyze(store, model, config):
    if store.get('work_categories') == []:
        return {'processed':0,'changes':0,'note':'尚未配置项目；可以直接检索聊天，或先添加项目。'}
    refresh_overview = store.get('overview_refresh', False) and not store.get('feedback_dirty',False)
    pending = [] if refresh_overview else store.rows("SELECT * FROM messages WHERE analyzed=0 ORDER BY ts DESC,id DESC LIMIT ?", (config["batch_size"],))
    pending.reverse()
    projects = store.projects()
    attachments = {p["id"]: project_context(p) for p in projects if p["status"] == "tracked" and p["paths"]}
    fingerprint = hashlib.sha256(json.dumps(attachments, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    dirty = refresh_overview or store.get("context_dirty", False) or (bool(attachments) and fingerprint != store.get("context_fingerprint"))
    if not pending and not dirty:
        return {"processed": 0, "changes": 0}
    # Include a little neighbouring context and existing cited evidence to support updates.
    related = {}
    chats = {m["chat_id"] for m in pending}
    # Current conversation context also anchors historical backfill and manual refreshes.
    chats.update(c['chat_id'] for p in projects if p['status'] != 'ignored' for c in p['chats'] if not c['excluded'])
    for chat in chats:
        for row in store.rows("SELECT * FROM messages WHERE chat_id=? ORDER BY ts DESC,id DESC LIMIT 24", (chat,)):
            related[row["id"]] = row
    for p in projects:
        refs = [ref for chat in p.get('overview', {}).get('conversations', []) for ref in chat.get('evidence', [])]
        refs += p["evidence"] + [ref for item in p["items"] for ref in item["evidence"]]
        for row in store.evidence(list(dict.fromkeys(refs))[-50:]):
            related[row["id"]] = row
        if p["status"] == "tracked":
            p["attached_context"] = attachments.get(p["id"], [])
    related.update({m["id"]: m for m in pending})
    if dirty and store.get('work_categories'):
        from .retrieval import project_context as retrieve
        for p in projects:
            context=retrieve(store,str(p['id']),limit=30)
            for m in store.evidence([m['id'] for m in context['messages']]):
                related[m['id']]=m
    data = {"now": datetime.now(CN).isoformat(), "projects": projects,
            "identity":store.get('identity',{}),
            "work_categories": store.get('work_categories',[]), "feedback": store.feedback(),
            "new_message_ids": [m["id"] for m in pending],
            "messages": [compact_message(m,store) for m in related.values()]}
    feedback_snapshot=data['feedback']
    task = "为所有未忽略的已有项目重写简洁概览，按各群聊/私聊最新上下文总结当前状态。即使事实未变化也输出新的 overview，不要逐条复述消息。本次只重写 summary/overview，保留项目 ID，不创建项目，items 返回空数组。" if refresh_overview else "结合有效用户反馈更新已有工作分类的当前状态与行动项。仅输出有实质变化的分类和事项，不把聊天片段逐条转成待办。"
    result = model.call(task +
                        "不要重新输出没有变化的事项。due、负责人必须有证据。尊重每个项目已排除的聊天。"
                        "历史回填可能晚于新消息到达：按消息时间还原进度，不要用旧消息覆盖更新的进展。", data)
    requested=list(dict.fromkeys(result.get('read_attachment_ids',[])))[:3]
    if requested:
        from .media import MediaReader
        from .retrieval import is_attachment
        reader=MediaReader(store.path.parent,config,store)
        for mid in requested:
            if mid not in related or not is_attachment(related[mid]):
                raise ValueError('模型请求的附件不在本批消息范围内。')
            cached=store.rows('SELECT * FROM media WHERE message_id=?',(mid,))
            start=json.loads(cached[0]['detail']).get('next_start',0) if cached else 0
            reader.read(mid,start or 0)
        data['messages']=[compact_message(m,store) for m in related.values()]
        result=model.call(task+' 已尝试读取你选择的附件，本轮读取预算已用完。结合读取结果重新整理；不要把尚未读完的附件当作读完。read_attachment_ids 留空。',data)
    changes = apply_analysis(store, result, related, [m["id"] for m in pending], overview_only=refresh_overview)
    feedback_changed=feedback_snapshot!=store.feedback()
    store.set("context_dirty", feedback_changed)
    store.set("overview_refresh", False)
    store.set("feedback_dirty", feedback_changed)
    store.set("context_fingerprint", fingerprint)
    store.set("analysis", {"ok": True, "at": int(time.time()), "processed": len(pending), "changes": changes})
    return {"processed": len(pending), "changes": changes}


def apply_analysis(store, result, messages, pending_ids, overview_only=False):
    """Invalid evidence must not silently acknowledge the input batch."""
    proposed = [dict(p, items=[]) if overview_only else p for p in result["projects"]]
    fixed={p['id'] for p in store.get('work_categories',[])}
    feedback={f['id']:f for f in store.feedback()}
    for p in proposed:
        if store.get('work_categories') is not None and p['id'] not in fixed:
            raise ValueError('只能更新用户已配置的项目。')
        if not p["name"].strip():
            raise ValueError("模型返回了无名称或无出处的项目，本批未标记为已分析。")
        for entry in [p, *p["items"]]:
            fids=entry.get('feedback_ids',[])
            if (not entry["evidence"] and not fids) or any(ref not in messages for ref in entry["evidence"]):
                raise ValueError("模型引用了本次输入以外的消息，本批未标记为已分析。")
            if any(fid not in feedback or feedback[fid]['project_id'] not in (None,p['id']) for fid in fids):
                raise ValueError('模型引用了无效或其他项目的反馈。')
        for item in p["items"]:
            if item["due"]:
                datetime.fromisoformat(item["due"])
            if item["state"] not in {"open", "reported_done", "done", "dismissed"} or item["kind"] not in {"task", "decision", "risk", "question"}:
                raise ValueError("模型返回了无效事项状态。")
            if item['state'] in {'done','dismissed'} and not item.get('feedback_ids'):
                raise ValueError('确认完成或忽略必须有用户反馈依据。')
            if item["priority"] not in {"normal", "urgent"} or not item["title"].strip():
                raise ValueError("模型返回了无效优先级或标题。")
        for index, chat in enumerate(p.get('overview', {}).get('conversations', []), 1):
            context = f'项目 ID {p["id"]} 的第 {index} 条会话概览'
            refs = chat['evidence']
            if not refs:
                raise ValueError(f'{context}没有消息引用，本批未标记为已分析。')
            missing = [ref for ref in refs if ref not in messages]
            if missing:
                raise ValueError(f'{context}引用了本次输入以外的消息 ID {missing[:10]}，本批未标记为已分析。')
            expected = chat.get('chat_id', messages[refs[0]]['chat_id'])
            wrong = [ref for ref in refs if messages[ref]['chat_id'] != expected]
            if wrong:
                raise ValueError(f'{context}引用了其他聊天的消息 ID {wrong[:10]}，本批未标记为已分析。')
            chat['chat_id'] = expected
    count, now = 0, int(time.time())
    with store.connect() as db:
        for p in proposed:
            old = db.execute("SELECT * FROM projects WHERE id=?", (p["id"],)).fetchone() if p["id"] else None
            if p["id"] and old is None:
                raise ValueError("模型使用了不存在的项目 ID。")
            if old is None:
                old = db.execute("SELECT * FROM projects WHERE name=?", (p["name"].strip(),)).fetchone()
            if overview_only and not old:
                raise ValueError('重写概览时不能创建新项目。')
            if old and old["status"] == "ignored":
                continue
            if old:
                pid = old["id"]
                excluded = {r[0] for r in db.execute("SELECT chat_id FROM project_chats WHERE project_id=? AND excluded=1", (pid,))}
            else:
                pid = db.execute("INSERT INTO projects(name,summary,evidence,updated) VALUES (?,?,?,?)",
                                 (p["name"].strip(), p["summary"], json.dumps(p["evidence"]), now)).lastrowid
                excluded = set()
                db.execute("INSERT INTO changes(project_id,body,ts) VALUES (?,?,?)", (pid, "发现项目候选：" + p["name"], now))
                count += 1
            refs = [ref for ref in p["evidence"] if messages[ref]["chat_id"] not in excluded]
            if not refs and not p.get('feedback_ids'):
                continue
            db.execute("UPDATE projects SET summary=?,evidence=?,updated=? WHERE id=?",
                       (p["summary"], json.dumps(refs), now, pid))
            if 'overview' in p:
                overview = dict(p['overview'])
                overview['conversations'] = [c for c in overview['conversations'] if c['chat_id'] not in excluded]
                db.execute('UPDATE projects SET overview=? WHERE id=?', (json.dumps(overview, ensure_ascii=False), pid))
            if old and old["summary"] != p["summary"] and not p["items"] and not overview_only:
                db.execute("INSERT INTO changes(project_id,body,ts) VALUES (?,?,?)", (pid, p["summary"], now))
                count += 1
            all_refs = refs + [r for item in p["items"] for r in item["evidence"]] + [r for c in p.get('overview', {}).get('conversations', []) for r in c['evidence']]
            for chat in {messages[r]["chat_id"] for r in all_refs} - excluded:
                db.execute("INSERT OR IGNORE INTO project_chats(project_id,chat_id) VALUES (?,?)", (pid, chat))
            for item in p["items"]:
                evidence = [ref for ref in item["evidence"] if messages[ref]["chat_id"] not in excluded]
                if not evidence and not item.get('feedback_ids'):
                    continue
                existing = db.execute("SELECT * FROM items WHERE id=? AND project_id=?", (item["id"], pid)).fetchone() if item["id"] else None
                if item["id"] and existing is None:
                    raise ValueError("模型引用了其他项目或不存在的待办。")
                if existing is None:
                    existing = db.execute("SELECT * FROM items WHERE project_id=? AND title=?", (pid, item["title"])).fetchone()
                if existing and existing["state"] in {"done", "dismissed"} and (not json.loads(existing['feedback_ids']) or not item.get('feedback_ids')):
                    continue
                fields = ("title", "kind", "owner", "due", "priority", "reason", "state")
                if existing and all(existing[k] == item[k] for k in fields) and json.loads(existing['feedback_ids'])==item.get('feedback_ids',[]):
                    continue
                values = [item[k] for k in fields]
                if existing:
                    iid = existing["id"]
                    db.execute("UPDATE items SET " + ",".join(k + "=?" for k in fields) + ",evidence=?,updated=? WHERE id=?",
                               (*values, json.dumps(evidence), now, iid))
                else:
                    iid = db.execute("INSERT INTO items(project_id," + ",".join(fields) + ",evidence,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                     (pid, *values, json.dumps(evidence), now)).lastrowid
                db.execute('UPDATE items SET feedback_ids=? WHERE id=?',(json.dumps(item.get('feedback_ids',[])),iid))
                recent = max((messages[r]["ts"] for r in evidence),default=0) > now - 6 * 3600
                urgent = item["priority"] == "urgent" and recent and item["state"] == "open"
                body = item["title"] + (" · 负责人：" + item["owner"] if item["owner"] else "")
                body += (" · 截止：" + item["due"] if item["due"] else "")
                if item["reason"]:
                    body += "\n" + item["reason"]
                db.execute("INSERT INTO changes(project_id,item_id,body,ts,urgent) VALUES (?,?,?,?,?)", (pid, iid, body, now, int(urgent)))
                count += 1
        if pending_ids:
            db.executemany("UPDATE messages SET analyzed=1 WHERE id=?", [(i,) for i in pending_ids])
    return count


def ask(store, model, question, project_id=None):
    # Literal search supports Chinese; project context also supplies recent related messages.
    rows = store.search(question, project_id, 60)
    if project_id:
        rows += store.rows("""SELECT * FROM messages WHERE chat_id IN
            (SELECT chat_id FROM project_chats WHERE project_id=? AND excluded=0)
            ORDER BY ts DESC LIMIT 80""", (project_id,))
    projects = [p for p in store.projects() if not project_id or p["id"] == project_id]
    if store.get('work_categories'):
        from .retrieval import project_context as retrieve
        selected=[p for p in projects if project_id or any(a.lower().replace(' ','') in question.lower().replace(' ','') for c in store.get('work_categories') if c['id']==p['id'] for a in [c['name'],*c['aliases']])]
        for p in selected:
            context=retrieve(store,str(p['id']),limit=60)
            rows+=store.evidence([m['id'] for m in context['messages']])
    if not rows:
        # Let the model pick search terms, rather than treating a whole question as a keyword.
        keywords = model.call("从问题提取最多四个用于聊天原文搜索的简短关键词，answer 用 | 分隔，evidence 为空。",
                              {"question": question}, ANSWER_SCHEMA)["answer"].split("|")[:4]
        for keyword in keywords:
            if keyword.strip():
                rows += store.search(keyword.strip(), project_id, 25)
    unique = {m["id"]: m for m in rows}
    for p in projects:
        for m in store.evidence(p["evidence"] + [r for i in p["items"] for r in i["evidence"]]):
            unique[m["id"]] = m
        if p["status"] == "tracked":
            p["attached_context"] = project_context(p)
    from .model import obj,REFS
    from .retrieval import is_attachment
    candidates=[compact_message(m,store) for m in unique.values() if is_attachment(m)]
    if candidates:
        selection=model.call('根据用户问题，从附件中选择最多三条最需要补读正文的消息 ID。已读完或明确缺失/失败的不选。不相关的图片不选。',
                             {'question':question,'attachments':candidates},obj({'ids':REFS}))
        from .media import MediaReader
        reader=MediaReader(store.path.parent,model.config,store)
        for mid in list(dict.fromkeys(selection['ids']))[:3]:
            if mid not in unique or not is_attachment(unique[mid]):raise ValueError('附件选择不在检索范围内。')
            cached=store.rows('SELECT detail FROM media WHERE message_id=?',(mid,))
            start=json.loads(cached[0]['detail']).get('next_start',0) if cached else 0
            reader.read(mid,start or 0)
    answer = model.call("回答用户问题。说明检索范围有限，不要声称已穷尽所有消息。给出下一步，并在文字中用 [消息ID] 标注出处。",
                        {"question": question, "projects": projects, "identity":store.get('identity',{}), "feedback":store.feedback(project_id), "messages": [compact_message(m,store) for m in unique.values()]}, ANSWER_SCHEMA)
    if any(r not in unique for r in answer["evidence"]):
        raise ValueError("回答引用了检索结果之外的消息，请重试。")
    answer["sources"] = store.evidence(answer["evidence"])
    return answer
