"""Local candidate discovery: return conversation metadata, never bulk chat bodies."""
import json
import time

from .work import resolve_project


def scope(store, query='', project=None, keywords=None):
    p=resolve_project(store,project) if project is not None else None
    words=[query,*(keywords or [])]
    if p:words.extend([p['name'],*p['aliases']])
    words=list(dict.fromkeys(w.strip() for w in words if w.strip()))
    linked={c['chat_id'] for c in p['chats'] if not c['excluded']} if p else set()
    excluded={c['chat_id'] for c in p['chats'] if c['excluded']} if p else set()
    return p,words,linked,excluded


def text_clause(words, columns=('m.text',)):
    # Literal substring matching: '%' and '_' supplied by users are not wildcards.
    return (' OR '.join(f'instr(lower({column}),lower(?))>0' for word in words for column in columns) or '0',
            [word for word in words for column in columns])


def chat_stats(store, days, words=(), chat_ids=None):
    floor=int(time.time())-max(1,days)*86400
    condition,args=text_clause(words,('m.text',"COALESCE(a.text,'')"))
    where='' if chat_ids is None else ' WHERE c.id IN (SELECT value FROM json_each(?))'
    params=(*args,*args,floor) if chat_ids is None else (*args,*args,floor,json.dumps(chat_ids))
    return store.rows(f'''SELECT c.id AS chat_id,c.name AS chat_name,c.kind,
        count(m.id) AS message_count,min(m.ts) AS earliest_ts,max(m.ts) AS latest_ts,
        sum(CASE WHEN {condition} THEN 1 ELSE 0 END) AS keyword_hits,
        max(CASE WHEN {condition} THEN m.id END) AS example_message_id
        FROM chats c LEFT JOIN messages m ON m.chat_id=c.id AND m.ts>=?
        LEFT JOIN media a ON a.message_id=m.id {where} GROUP BY c.id''',params)


def page(candidates,offset,limit):
    offset=max(0,offset);limit=max(1,min(limit,50))
    selected=candidates[offset:offset+limit]
    return dict(chats=selected,total=len(candidates),
                next_offset=offset+len(selected) if offset+len(selected)<len(candidates) else None)


def discover_chats(store,query='',project=None,keywords=None,days=30,include_content_matches=False,offset=0,limit=20):
    p,words,linked,excluded=scope(store,query,project,keywords)
    if not words:raise ValueError('请提供要查的项目、名称或关键词。')
    candidates=[]
    for chat in chat_stats(store,days,words if include_content_matches else []):
        if chat['chat_id'] in excluded:continue
        reasons=[];rank=0
        if any(w.casefold() in (chat['chat_name'] or '').casefold() for w in words):
            reasons.append('群名匹配' if chat['kind']=='group' else '联系人名称匹配')
            rank=3 if chat['kind']=='group' else 2
        if chat['chat_id'] in linked:
            reasons.append('已有项目关联（仍需核对内容）');rank=4
        if include_content_matches and chat['keyword_hits']:
            reasons.append('消息或已缓存附件命中关键词');rank=max(rank,1)
        if not reasons:continue
        candidates.append(chat|dict(reasons=reasons,rank=rank))
    candidates.sort(key=lambda c:(-c['rank'],-c['keyword_hits'],-(c['latest_ts'] or 0),c['chat_id']))
    result=page(candidates,offset,limit)
    return result|dict(terms=words,days=days,content_searched=include_content_matches,
        feedback=store.feedback(p['id']) if p else [f for f in store.feedback() if f['project_id'] is None],
        note='仅返回候选会话元数据，未阅读正文。先核对强相关群，再查群成员私聊；用户可补充遗漏会话。名称或成员关系不是项目事实。无近期消息不代表没有更早记录。')


def find_private_chats(store,group_ids,query='',project=None,keywords=None,days=30,offset=0,limit=20):
    groups=list(dict.fromkeys(group_ids))
    if not 1<=len(groups)<=10:raise ValueError('每次指定 1–10 个已判断为强相关的群聊。')
    p,words,linked,excluded=scope(store,query,project,keywords)
    directory={c['id']:c for c in store.rows('SELECT id,name,kind FROM chats')}
    evidence={};coverage=[]
    floor=int(time.time())-max(1,days)*86400
    for gid in groups:
        group=directory.get(gid)
        if not group or group['kind']!='group':raise ValueError('group_ids 中包含不存在或非群聊的会话。')
        if gid in excluded:continue
        account=gid.split(':',1)[0]
        status=store.get('directory:'+account,{})
        self_id=account+':'+status.get('self_username',store.get('identity',{}).get('username',''))
        roster={r['member_chat_id'] for r in store.rows('SELECT member_chat_id FROM chat_members WHERE group_id=?',(gid,))}
        observed=store.rows('''SELECT json_extract(locator,'$.sender_username') AS username,
            count(*) AS messages,max(id) AS evidence_id FROM messages WHERE chat_id=? AND ts>=?
            AND COALESCE(json_extract(locator,'$.sender_username'),'')!='' GROUP BY username''',(gid,floor))
        speakers={account+':'+r['username']:r for r in observed}
        coverage.append(dict(group_id=gid,roster_available=status.get('roster_available',False),
            roster_members=len(roster),observed_speakers=len(speakers),synced_at=status.get('at'),
            note='群成员快照及近期发言人均可能不完整；未发言且缺失成员表的人无法发现。'))
        for cid in roster|speakers.keys():
            if cid==self_id or cid in excluded or cid not in directory or directory[cid]['kind']!='private':continue
            speaker=speakers.get(cid)
            evidence.setdefault(cid,[]).append(dict(group_id=gid,group_name=group['name'],
                source='roster_and_speaker' if cid in roster and speaker else 'roster' if cid in roster else 'observed_speaker',
                group_message_count=speaker['messages'] if speaker else 0,
                evidence_message_id=speaker['evidence_id'] if speaker else None))
    # Search content only in identified member conversations, not every private chat.
    stats={c['chat_id']:c for c in chat_stats(store,days,words,chat_ids=list(evidence))}
    candidates=[stats[cid]|dict(member_evidence=refs,linked=cid in linked,
        relevance='keyword_match' if stats[cid]['keyword_hits'] else 'membership_only') for cid,refs in evidence.items()]
    candidates.sort(key=lambda c:(-bool(c['keyword_hits']),-c['linked'],-c['keyword_hits'],
        -sum(r['group_message_count'] for r in c['member_evidence']),-(c['latest_ts'] or 0),c['chat_id']))
    return page(candidates,offset,limit)|dict(terms=words,days=days,coverage=coverage,
        note='通过同一账户下的微信标识关联，不按同名猜测。这里只提供候选私聊和命中数量，不读取全部正文或自动关联项目。membership_only 需要关键词/少量上下文核实；message_count=0 表示此时间范围无已入库消息。')


def set_project_chats(store,project,chat_ids,excluded=False):
    p=resolve_project(store,project)
    ids=list(dict.fromkeys(chat_ids))
    with store.connect() as db:
        for cid in ids:
            if not db.execute('SELECT 1 FROM chats WHERE id=?',(cid,)).fetchone():raise ValueError('会话不存在。')
        db.executemany('INSERT OR REPLACE INTO project_chats VALUES (?,?,?)',[(p['id'],cid,int(excluded)) for cid in ids])
    store.set('context_dirty',True)
    return dict(project_id=p['id'],chat_ids=ids,excluded=bool(excluded))
