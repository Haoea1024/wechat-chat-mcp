"""User-defined projects, persisted in the local data directory."""
import hashlib
import json
import time

from .config import load, save


def _fingerprint(projects):
    return hashlib.sha256(json.dumps(projects,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def initialize_work(store):
    root, config = load(store.path.parent)
    if 'projects' not in config:
        # Existing installations keep their categories and feedback in local storage.
        previous = {p['id']:p for p in store.projects(include_archived=True)}
        config['projects'] = [dict(id=c['id'],name=previous[c['id']]['name'],aliases=c.get('aliases',[]),
                                   notes=previous[c['id']]['notes'],status=previous[c['id']]['status'])
                              for c in store.get('work_categories',[]) if c['id'] in previous]
    definitions = config['projects']
    if store.get('projects_config_fingerprint') == _fingerprint(definitions):
        return
    names = [p['name'].strip() for p in definitions]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError('项目名称不能为空或重复。')
    categories=[];normalized=[]
    with store.connect() as db:
        for definition,name in zip(definitions,names):
            aliases=definition.get('aliases',[])
            status=definition.get('status','tracked')
            if not isinstance(aliases,list) or any(not isinstance(a,str) for a in aliases):
                raise ValueError('项目 aliases 应为字符串列表。')
            if status not in {'tracked','ignored'}:
                raise ValueError('项目 status 应为 tracked 或 ignored。')
            old=db.execute('SELECT * FROM projects WHERE id=?',(definition.get('id'),)).fetchone()
            if old is None:old=db.execute('SELECT * FROM projects WHERE name=?',(name,)).fetchone()
            notes=definition.get('notes',old['notes'] if old else '')
            if old:
                pid=old['id']
                db.execute('UPDATE projects SET name=?,notes=?,status=?,updated=? WHERE id=?',(name,notes,status,int(time.time()),pid))
            else:
                pid=db.execute('INSERT INTO projects(name,notes,status,summary,updated) VALUES (?,?,?,?,?)',
                               (name,notes,status,'尚未整理进度；可以先通过智能体检索相关聊天。',int(time.time()))).lastrowid
            categories.append(dict(id=pid,name=name,aliases=aliases))
            normalized.append(dict(id=pid,name=name,aliases=aliases,notes=notes,status=status))
        active={c['id'] for c in categories}
        for old in db.execute('SELECT id FROM projects').fetchall():
            if old['id'] not in active:db.execute("UPDATE projects SET status='ignored' WHERE id=?",(old['id'],))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('work_categories',json.dumps(categories,ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('projects_config_fingerprint',json.dumps(_fingerprint(normalized))))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('context_dirty','true'))
    config['projects']=normalized
    save(root,config)


def configure_project(store,name,aliases=None,notes=None,project_id=None,status=None):
    """Add or edit only a project explicitly supplied by the user."""
    initialize_work(store)
    root,config=load(store.path.parent)
    name=name.strip()
    if not name:raise ValueError('请填写项目名称。')
    if status is not None and status not in {'tracked','ignored'}:raise ValueError('项目状态应为 tracked 或 ignored。')
    projects=config['projects']
    existing=next((p for p in projects if (p['id']==project_id if project_id else p['name']==name)),None)
    if project_id and not existing:raise ValueError('找不到要修改的项目。')
    if any(p['name']==name and p is not existing for p in projects):raise ValueError('项目名称已存在。')
    value=dict(existing or {},name=name)
    if aliases is not None:value['aliases']=list(dict.fromkeys(a.strip() for a in aliases if a.strip()))
    if notes is not None:value['notes']=notes
    if status is not None:value['status']=status
    if existing:projects[projects.index(existing)]=value
    else:projects.append(value)
    save(root,config)
    initialize_work(store)
    return next(p for p in store.projects() if p['name']==name)


def persist_projects(store):
    """Keep page edits and the local editable configuration in agreement."""
    root,config=load(store.path.parent)
    current={p['id']:p for p in store.rows('SELECT id,name,notes,status FROM projects')}
    config['projects']=[dict(current[c['id']],aliases=c.get('aliases',[])) for c in store.get('work_categories',[]) if c['id'] in current]
    save(root,config)
    initialize_work(store)


def resolve_project(store, name):
    projects=store.projects()
    if isinstance(name,int) or str(name).isdigit():
        matches=[p for p in projects if p['id']==int(name)]
    else:
        query=str(name).lower().replace(' ','')
        categories=store.get('work_categories',[])
        exact={c['id'] for c in categories if any(query==s.lower().replace(' ','') for s in [c['name'],*c.get('aliases',[])])}
        ids=exact or {c['id'] for c in categories if query and any(query in s.lower().replace(' ','') or s.lower().replace(' ','') in query for s in [c['name'],*c.get('aliases',[])] if s.strip())}
        matches=[p for p in projects if p['id'] in ids or query==p['name'].lower().replace(' ','')]
    if len(matches)!=1:
        raise ValueError('请先用 list_projects 查看项目；也可用 configure_project 添加项目，或直接 search_chat 搜索聊天。')
    return matches[0]
