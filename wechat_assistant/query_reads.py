"""Query-scoped references, never a global 'already read' filter or project coverage."""
import secrets
import time

TTL = 24*3600


def start(store):
    now=int(time.time());query_id=secrets.token_hex(16)
    with store.connect() as db:
        db.execute('DELETE FROM query_reads WHERE query_id IN (SELECT id FROM query_sessions WHERE created<=?)',(now-TTL,))
        db.execute('DELETE FROM query_sessions WHERE created<=?',(now-TTL,))
        db.execute('INSERT INTO query_sessions VALUES (?,?)',(query_id,now))
    return query_id


def finish(store,result,query_id=None,force=False):
    if not query_id:return result
    messages=result if isinstance(result,list) else result.get('messages',[])
    returned=[]
    with store.connect() as db:
        session=db.execute('SELECT created FROM query_sessions WHERE id=?',(query_id,)).fetchone()
        if not session or session['created']<=int(time.time())-TTL:
            raise ValueError('本次查询标识不存在或已过期，请重新调用project_status取得query_id后重读；不能沿用其他对话的标识。')
        for message in messages:
            source=message.get('review_source')
            # Truncated messages are never registered or suppressed as fully read.
            if not source or message.get('text_truncated'):
                returned.append(message);continue
            old=db.execute('SELECT digest,media_digest FROM query_reads WHERE query_id=? AND message_id=?',
                           (query_id,message['id'])).fetchone()
            if not force and old and old['digest']==source['digest'] and old['media_digest']==source['media_digest']:
                returned.append(dict(id=message['id'],chat_id=message['chat_id'],ts=message['ts'],
                                     already_returned=True,text_omitted=True))
            else:
                returned.append(message)
                db.execute('INSERT OR REPLACE INTO query_reads VALUES (?,?,?,?)',
                           (query_id,message['id'],source['digest'],source['media_digest']))
    # References retain IDs and original pagination; nothing is silently filtered out.
    return returned if isinstance(result,list) else result|dict(messages=returned)
