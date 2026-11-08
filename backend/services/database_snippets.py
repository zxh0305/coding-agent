"""Durable SQL snippets, scoped to the same application user and project."""
import json,os,time,uuid
from pathlib import Path
from services.databases import _LOCK
import db

def _path():return db.DB_PATH.parent/'database-snippets.json'
def _read():return json.loads(_path().read_text()) if _path().exists() else []
def _scope(uid,workspace):return uid,str(Path(workspace).resolve())
def list_snippets(uid,workspace):
    uid,workspace=_scope(uid,workspace)
    with _LOCK:return sorted([x for x in _read() if x['owner']==uid and x['workspace']==workspace],key=lambda x:x['updated'],reverse=True)
def save(uid,workspace,body):
    uid,workspace=_scope(uid,workspace);name=body.get('name');sql=body.get('sql')
    if not isinstance(name,str) or not name.strip() or len(name)>100:raise ValueError('请输入名称（最多100字）')
    if not isinstance(sql,str) or not sql.strip() or len(sql)>32000:raise ValueError('请输入 SQL（最多32000字符）')
    with _LOCK:
        items=_read();ident=body.get('snippet_id')
        if ident and not any(x['id']==ident and x['owner']==uid and x['workspace']==workspace for x in items):raise ValueError('常用 SQL 不存在或不属于当前项目')
        ident=ident or uuid.uuid4().hex
        item={'id':ident,'owner':uid,'workspace':workspace,'name':name.strip(),'sql':sql,'updated':time.time()}
        items=[x for x in items if x['id']!=ident];items.append(item);_write(items)
    return ident
def remove(uid,workspace,ident):
    uid,workspace=_scope(uid,workspace)
    with _LOCK:_write([x for x in _read() if not(x['id']==ident and x['owner']==uid and x['workspace']==workspace)])
def _write(items):
    path=_path();path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as out:json.dump(items,out,ensure_ascii=False)
    os.replace(temp,path);os.chmod(path,0o600)
