"""Local counters only: no message contents, credentials or client billing guesses."""
import json
import logging
import math
from datetime import datetime, timedelta, timezone


BEIJING = timezone(timedelta(hours=8))
COUNTERS = ('calls','errors','input_chars','output_chars','input_estimate','output_estimate',
            'images','reported_calls','input_tokens','output_tokens')
SCOPE = ('MCP仅统计本服务处理的工具调用；文字token按非ASCII字符1个、ASCII字符约4个折算，'
         '是内容体量参考，不是模型分词或实际扣费。不含历史上下文重发、系统提示、工具定义、'
         '客户端思考与回答、图像token或客户端自行打开的文件。重复调用重复计数，结构化副本不重复计数。'
         '独立模型用量单列，不与MCP估算相加；缺少usage的请求标为未知。积分、费用和剩余额度请看客户端账单。')


def text_size(text):
    ascii_count=sum(ord(c)<128 for c in text)
    return len(text), math.ceil(ascii_count/4)+len(text)-ascii_count


def result_size(result):
    """Count the text representation once, not its duplicate structuredContent."""
    if isinstance(result,tuple):result=result[0]
    if hasattr(result,'content'):result=result.content
    if isinstance(result,dict):result=[dict(type='text',text=json.dumps(result,ensure_ascii=False))]
    chars=estimate=images=0
    for block in result or []:
        value=block if isinstance(block,dict) else block.model_dump()
        if value.get('type')=='image':images+=1
        elif value.get('type')=='text':
            c,t=text_size(value['text']);chars+=c;estimate+=t
        elif value.get('type')=='resource' and 'text' in value.get('resource',{}):
            c,t=text_size(value['resource']['text']);chars+=c;estimate+=t
    return chars,estimate,images


def reported_usage(usage):
    """Missing/malformed usage is unknown, never an invented zero-token call."""
    if not isinstance(usage,dict):return {}
    prompt,completion=usage.get('prompt_tokens'),usage.get('completion_tokens')
    if any(type(n) is not int or n<0 for n in (prompt,completion)):return {}
    return dict(reported_calls=1,input_tokens=prompt,output_tokens=completion)


def record(store,source,operation,*,now=None,**values):
    # A statistics failure must not turn a successful save into an apparent failure/retry.
    try:
        day=(now or datetime.now(BEIJING)).astimezone(BEIJING).date().isoformat()
        counts=[values.get(k,0) for k in COUNTERS]
        columns=','.join(COUNTERS)
        updates=','.join(f'{k}={k}+excluded.{k}' for k in COUNTERS)
        with store.connect() as db:
            db.execute(f'INSERT INTO usage_daily(day,source,operation,{columns}) VALUES ({",".join("?" for _ in range(3+len(COUNTERS)))}) '
                       f'ON CONFLICT(day,source,operation) DO UPDATE SET {updates}',[day,source,operation,*counts])
    except Exception:
        logging.getLogger(__name__).warning('本次用量未能记录；业务结果不受影响。')


def summary(store,days=7,now=None):
    if type(days) is not int or not 0<=days<=365:raise ValueError('days须为0（全部）或1–365天')
    today=(now or datetime.now(BEIJING)).astimezone(BEIJING).date()
    since=(today-timedelta(days=days-1)).isoformat() if days else '0000-01-01'
    sums=','.join(f'SUM({k}) AS {k}' for k in COUNTERS)
    rows=store.rows(f'SELECT source,operation,{sums} FROM usage_daily WHERE day>=? AND day<=? '
                    'GROUP BY source,operation ORDER BY SUM(output_estimate) DESC,source,operation',(since,today.isoformat()))
    for row in rows:
        if not row['reported_calls']:
            row['input_tokens']=row['output_tokens']=None
    def totals(selected):
        result={k:sum(r[k] or 0 for r in selected) for k in COUNTERS}
        result['unreported_calls']=result['calls']-result['reported_calls']
        # No provider receipt is different from a real receipt reporting zero tokens.
        if not result['reported_calls']:
            for k in ('input_tokens','output_tokens'):result[k]=None
        return result
    return dict(days=days,timezone='Asia/Shanghai',started_at=store.get('usage_started'),
                mcp=totals([r for r in rows if r['source']=='mcp']),
                model=totals([r for r in rows if r['source']!='mcp']),by_operation=rows,
                client_quota=None,note=SCOPE)
