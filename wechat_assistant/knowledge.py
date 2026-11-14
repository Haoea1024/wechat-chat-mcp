"""Revisable project notes with evidence, exact-message coverage and original-source access."""
import hashlib
import json
import time
from dataclasses import dataclass,field
from typing import Literal

from .discovery import scope,text_clause
from .work import resolve_project


@dataclass
class ReviewSource:
    id: int
    digest: str
    media_digest: str = ''


@dataclass
class BriefEntry:
    key: str
    section: Literal['background','status','decision','next_step','question']
    text: str
    basis: Literal['reported','inferred','open']
    evidence_ids: list[int] = field(default_factory=list)
    feedback_ids: list[int] = field(default_factory=list)
    attachment_tokens: list[str] = field(default_factory=list)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def message_digest(message):
    return digest({k:message[k] for k in ('id','chat_id','sender','ts','kind','text','raw')})


def media_digest(record):
    if not record:return ''
    detail=json.loads(record['detail'])
    return digest(dict(status=record['status'],text=record['text'],
                       parts=detail.get('parts'),members=detail.get('members')))


def review_source(store,message):
    media=store.rows('SELECT * FROM media WHERE message_id=?',(message['id'],))
    return dict(id=message['id'],digest=message_digest(message),media_digest=media_digest(media[0] if media else None))


def receipt_content(record,coverage):
    detail=json.loads(record['detail'])
    if coverage['member']:
        parts=detail.get('members',{}).get(coverage['member'],{}).get('parts',{})
    else:parts=detail.get('parts')
    if coverage['indices'] is None:return [record['text']]
    return [[i,parts.get(str(i)) if parts is not None else None] for i in coverage['indices']]


def record_attachment_receipt(store,message,result,start=0,member=None):
    """Only actual returned content creates a receipt; a ZIP listing is not a reading."""
    if result.get('status') not in {'ready','partial'}:return result
    rows=store.rows('SELECT * FROM media WHERE message_id=?',(message['id'],))
    if not rows:return result
    record=rows[0];detail=json.loads(record['detail'])
    if detail.get('archive_members_only') and not member:return result
    parts=detail.get('members',{}).get(member,{}) if member else detail
    indices=None if 'parts' not in parts else [i for i in range(start,min(start+5,parts.get('total_units',0))) if str(i) in parts['parts']]
    coverage=dict(member=member,indices=indices,total_units=parts.get('total_units'),
                  note='仅表示本次返回的页/段；下标从0开始，不代表整份附件读完。')
    if indices==[] or not result.get('text'):return result
    body=dict(message_id=message['id'],message_digest=message_digest(message),coverage=coverage,
              content_digest=digest(receipt_content(record,coverage)))
    token=digest(body)
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO attachment_receipts VALUES (?,?,?)',
                   (token,message['id'],json.dumps(body,ensure_ascii=False)))
    return result|dict(attachment_receipt=dict(token=token,**body))


def receipt_current(store,token):
    rows=store.rows('SELECT body FROM attachment_receipts WHERE token=?',(token,))
    if not rows:return None
    receipt=json.loads(rows[0]['body']);messages=store.evidence([receipt['message_id']])
    if receipt['coverage'].get('kind') == 'client_content':
        if not messages or message_digest(messages[0])!=receipt['message_digest']:return None
        from .client_media import file_digest
        try:
            if any(file_digest(path)!=value for path,value in receipt['files'].items()):return None
        except OSError:return None
        return receipt
    media=store.rows('SELECT * FROM media WHERE message_id=?',(receipt['message_id'],))
    if not messages or not media or media[0]['status'] not in {'ready','partial','listed'}:return None
    if message_digest(messages[0])!=receipt['message_digest']:return None
    if digest(receipt_content(media[0],receipt['coverage']))!=receipt['content_digest']:return None
    return receipt


def _brief(store,pid,version=None):
    rows=store.rows('SELECT * FROM project_briefs WHERE project_id=?'+(' AND version=?' if version is not None else '')+
                    ' ORDER BY version DESC LIMIT 1',(pid,version) if version is not None else (pid,))
    if version is not None and not rows:raise ValueError('找不到这个资料版本。')
    return (json.loads(rows[0]['body']),rows[0]['version'],rows[0]['created']) if rows else ({'entries':{},'feedback_digest':'','scope_note':''},0,None)


def save_brief(store,project,expected_version,entries,reviewed_sources,scope_note,archive_keys=None):
    p=resolve_project(store,project);pid=p['id']
    current,version,_=_brief(store,pid)
    if version!=expected_version:raise ValueError('项目资料已更新，请先读取最新版本后合并。')
    if not scope_note.strip():raise ValueError('请说明阅读范围和未覆盖的内容，不要把局部阅读写成完整审阅。')
    excluded={c['chat_id'] for c in p['chats'] if c['excluded']}
    reviewed={r['message_id']:r for r in store.rows('SELECT * FROM project_reviewed WHERE project_id=?',(pid,))}
    updates={}
    for source in reviewed_sources:
        rows=store.evidence([source['id']])
        if not rows or rows[0]['chat_id'] in excluded or message_digest(rows[0])!=source['digest']:
            raise ValueError('阅读凭据对应的消息已变化、被排除或不存在，请补读后再保存。')
        updates[source['id']]=dict(message_id=source['id'],digest=source['digest'],media_digest=source.get('media_digest',''))
    reviewed.update(updates)
    feedback={f['id']:f for f in store.feedback(pid)}
    merged=dict(current['entries'])
    seen=set()
    for entry in entries:
        key=entry['key'].strip()
        if not key or key in seen or not entry['text'].strip():raise ValueError('条目 key 和正文不能为空，key 不能重复。')
        seen.add(key)
        if entry['section'] not in {'background','status','decision','next_step','question'} or entry['basis'] not in {'reported','inferred','open'}:
            raise ValueError('条目的 section 或 basis 无效。')
        evidence=list(dict.fromkeys(entry.get('evidence_ids',[])))
        feedback_ids=list(dict.fromkeys(entry.get('feedback_ids',[])))
        tokens=list(dict.fromkeys(entry.get('attachment_tokens',[])))
        if entry['basis']!='open' and not (evidence or feedback_ids or tokens):
            raise ValueError('事实记录与推测必须附出处；待核实问题可标为 open。')
        snapshots={}
        for mid in evidence:
            rows=store.evidence([mid]);read=reviewed.get(mid)
            if not rows or rows[0]['chat_id'] in excluded or not read or read['digest']!=message_digest(rows[0]):
                raise ValueError('引用的消息尚未完整返回，或原文已变化；请 get_messages 补读。')
            snapshots[str(mid)]=read['digest']
        for fid in feedback_ids:
            if fid not in feedback:raise ValueError('不能引用其他项目、已撤销或不存在的反馈。')
        attachments=[]
        for token in tokens:
            receipt=receipt_current(store,token)
            if not receipt or store.evidence([receipt['message_id']])[0]['chat_id'] in excluded:
                raise ValueError('附件凭据失效、被排除或正文未读；请重新读取对应部分。')
            attachments.append(dict(token=token,message_id=receipt['message_id'],coverage=receipt['coverage']))
            mid=receipt['message_id']
            if mid in reviewed and reviewed[mid]['digest']==receipt['message_digest']:
                media=store.rows('SELECT * FROM media WHERE message_id=?',(mid,))[0]
                updates[mid]=reviewed[mid]|dict(media_digest=media_digest(media))
        merged[key]=dict(key=key,section=entry['section'],text=entry['text'].strip(),basis=entry['basis'],
            evidence_ids=evidence,feedback_ids=feedback_ids,attachments=attachments,
            evidence_digests=snapshots,updated=int(time.time()))
    for key in archive_keys or []:
        if key in seen:raise ValueError('同一次保存不能更新并归档同一个条目。')
        if key not in merged:raise ValueError('要归档的条目不存在。')
        del merged[key]
    body=dict(entries=merged,scope_note=scope_note.strip(),feedback_digest=digest(list(feedback.values())))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        latest=db.execute('SELECT COALESCE(max(version),0) FROM project_briefs WHERE project_id=?',(pid,)).fetchone()[0]
        if latest!=expected_version:raise ValueError('项目资料已更新，请先合并最新版本。')
        # Recheck message versions under the write lock before committing reading coverage.
        for read in updates.values():
            row=db.execute('SELECT * FROM messages WHERE id=?',(read['message_id'],)).fetchone()
            if row is None or message_digest(dict(row))!=read['digest']:raise ValueError('保存期间原文发生变化，请补读。')
        db.execute('INSERT INTO project_briefs VALUES (?,?,?,?)',(pid,version+1,json.dumps(body,ensure_ascii=False),int(time.time())))
        db.executemany('INSERT OR REPLACE INTO project_reviewed VALUES (?,?,?,?)',
                       [(pid,r['message_id'],r['digest'],r['media_digest']) for r in updates.values()])
    return dict(saved=True,project_id=pid,version=version+1,entries=len(merged),reviewed_messages=len(updates),
        note='未提供的条目继续保留；旧版本和原始消息未删除。助手资料不等于用户确认。')


def pending_messages(store,project,chat_ids=None):
    p,words,linked,excluded=scope(store,project=project)
    # Previously reviewed chats remain candidates, not user-confirmed associations.
    known={r['chat_id'] for r in store.rows('''SELECT DISTINCT m.chat_id FROM project_reviewed r
        JOIN messages m ON m.id=r.message_id WHERE r.project_id=?''',(p['id'],))}
    clause,args=text_clause(words,('m.text',"COALESCE(a.text,'')"))
    where='('+clause+' OR m.chat_id IN (SELECT value FROM json_each(?)))';args.append(json.dumps(sorted(linked|known)))
    if chat_ids is not None:
        where='m.chat_id IN (SELECT value FROM json_each(?))';args=[json.dumps(chat_ids)]
    if excluded:where+=' AND m.chat_id NOT IN (SELECT value FROM json_each(?))';args.append(json.dumps(sorted(excluded)))
    rows=store.rows('SELECT m.* FROM messages m LEFT JOIN media a ON a.message_id=m.id WHERE '+where+' ORDER BY m.id DESC',args)
    covered={r['message_id']:r for r in store.rows('SELECT * FROM project_reviewed WHERE project_id=?',(p['id'],))}
    media={r['message_id']:r for r in store.rows('SELECT * FROM media WHERE message_id IN (SELECT value FROM json_each(?))',
        (json.dumps([m['id'] for m in rows]),))}
    pending=[]
    for m in rows:
        previous=covered.get(m['id'])
        reason='unreviewed' if previous is None else 'message_changed' if previous['digest']!=message_digest(m) else 'attachment_changed' if previous['media_digest']!=media_digest(media.get(m['id'])) else None
        if reason:pending.append((m,reason))
    return pending


def read_updates(store,project,chat_ids=None,before_id=None,limit=20,max_chars=12000):
    from .retrieval import bounded_messages
    pending=pending_messages(store,project,chat_ids)
    candidates=[(m,r) for m,r in pending if before_id is None or m['id']<before_id]
    selected=candidates[:max(1,min(limit,100))]
    messages=bounded_messages(store,[m|dict(update_reason=r) for m,r in selected],max_chars)
    return dict(messages=messages,total_pending=len(pending),
        next_before_id=messages[-1]['id'] if len(candidates)>len(messages) else None,
        note='优先新入库消息，包含迟到旧消息；用next_before_id继续，新一轮不传before_id。保存资料才推进覆盖。待查数可含历史积压，不要求全部读完；已引用原文变化应按资料的needs_review直接补读。附件变化需按需查证。')


def get_brief(store,project,version=None,offset=0,limit=20):
    p=resolve_project(store,project);body,current,created=_brief(store,p['id'],version)
    feedback=store.feedback(p['id']);valid_feedback={f['id'] for f in feedback}
    excluded={c['chat_id'] for c in p['chats'] if c['excluded']}
    entries=[]
    for entry in body['entries'].values():
        reasons=[]
        for mid,expected in entry['evidence_digests'].items():
            rows=store.evidence([int(mid)])
            if not rows or message_digest(rows[0])!=expected:reasons.append('引用原文已变化或缺失')
            elif rows[0]['chat_id'] in excluded:reasons.append('引用会话已被排除')
        if set(entry['feedback_ids'])-valid_feedback:reasons.append('引用反馈已撤销')
        for attachment in entry['attachments']:
            receipt=receipt_current(store,attachment['token'])
            if not receipt:reasons.append('引用附件内容已变化或读取缓存缺失')
            elif store.evidence([receipt['message_id']])[0]['chat_id'] in excluded:reasons.append('引用附件所在会话已被排除')
        entries.append({k:v for k,v in entry.items() if k!='evidence_digests'}|dict(needs_review=bool(reasons),review_reasons=list(dict.fromkeys(reasons))))
    offset=max(0,offset);limit=max(1,min(limit,50));selected=entries[offset:offset+limit]
    pending=pending_messages(store,project)
    ids={int(mid) for e in selected for mid in e['evidence_ids']}|{a['message_id'] for e in selected for a in e['attachments']}
    assets=[]
    for mid in sorted(ids):
        rows=store.rows('SELECT status,detail FROM media WHERE message_id=?',(mid,))
        backups=store.rows('SELECT name,path,sha256,quality,size,message_digest FROM attachment_backups WHERE message_id=?',(mid,))
        messages=store.evidence([mid])
        if rows or backups or (messages and messages[0]['kind'] in {'图片','语音','链接/文件'}):
            assets.append(dict(message_id=mid,reading_status=rows[0]['status'] if rows else 'not_read',
                total_units=json.loads(rows[0]['detail']).get('total_units') if rows else None,
                backups=[{k:v for k,v in b.items() if k!='message_digest'}|dict(matches_current_message=bool(messages and message_digest(messages[0])==b['message_digest'])) for b in backups]))
    return dict(project_id=p['id'],version=current,created=created,entries=selected,total_entries=len(entries),
        next_offset=offset+len(selected) if offset+len(selected)<len(entries) else None,
        scope_note=body['scope_note'],feedback_changed=bool(current and body['feedback_digest']!=digest(feedback)),
        active_feedback_ids=[f['id'] for f in feedback],pending_messages=len(pending),assets=assets,
        note='助手整理的可修订资料，不是用户确认或最新事实保证。先检查新增消息与有效反馈；遇到矛盾、细节或重大决定回查原文/附件。原文始终可读；备份位置不代表文件已再次校验。')
