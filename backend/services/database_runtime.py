"""Live database handles for the workbench; serialized per user/project/connection."""
import threading,time
from pathlib import Path
import db
from services import databases as config
_HANDLES={};_LOCK=threading.RLock();_REAPER_STARTED=False
def _reap():
    while True:
        threading.Event().wait(60)
        with _LOCK:entries=list(_HANDLES.values())
        for entry in entries:
            if time.monotonic()-entry['used']>900 and entry['lock'].acquire(blocking=False):
                try:
                    if time.monotonic()-entry['used']>900:_close(entry)
                finally:entry['lock'].release()

def _key(uid,workspace,ident):return (str(db.DB_PATH),uid,str(Path(workspace).resolve()),ident)
def _entry(uid,workspace,ident):
    config._get(uid,workspace,ident)
    global _REAPER_STARTED
    with _LOCK:
        if not _REAPER_STARTED:
            threading.Thread(target=_reap,daemon=True,name='database-idle-cleanup').start();_REAPER_STARTED=True
        return _HANDLES.setdefault(_key(uid,workspace,ident),{'lock':threading.RLock(),'conn':None,'used':time.monotonic()})
def _close(entry):
    if entry['conn'] is not None:
        try:entry['conn'].close()
        except Exception:pass
    entry['conn']=None

def _alive(entry):
    conn=entry['conn']
    if conn is None:return False
    try:
        if time.monotonic()-entry['used']>900:_close(entry);return False
        if hasattr(conn,'ping'):conn.ping(reconnect=False)
        else:conn.execute('SELECT 1').close()
        return True
    except Exception:_close(entry);return False

def status(uid,workspace,ident):
    entry=_entry(uid,workspace,ident)
    with entry['lock']:return {'connected':_alive(entry)}
def disconnect(uid,workspace,ident):
    entry=_entry(uid,workspace,ident)
    with entry['lock']:_close(entry)
    return {'connected':False}
def connect(uid,workspace,ident):
    entry=_entry(uid,workspace,ident)
    with entry['lock']:
        if not _alive(entry):
            try:entry['conn']=config._connect(config._get(uid,workspace,ident),writable=True)
            except Exception:raise ValueError('连接失败，请检查账号、密码、数据库地址和驱动安装') from None
        entry['used']=time.monotonic()
        return {'connected':True}
def forget(uid,workspace,ident):
    with _LOCK:entry=_HANDLES.pop(_key(uid,workspace,ident),None)
    if entry:
        with entry['lock']:_close(entry)
def execute(uid,workspace,ident,sql):
    if not isinstance(sql,str) or not sql.strip() or len(sql)>32000:raise ValueError('请输入 SQL，每次执行一条语句')
    item=config._get(uid,workspace,ident);entry=_entry(uid,workspace,ident)
    with entry['lock']:
        cursor=None
        try:
            connect(uid,workspace,ident);conn=entry['conn'];entry['used']=time.monotonic()
            if item['kind']=='sqlite':
                deadline=time.monotonic()+10;conn.set_progress_handler(lambda:time.monotonic()>deadline,1000)
            cursor=conn.cursor();started=time.monotonic();cursor.execute(sql)
            columns=[col[0] for col in cursor.description or []];rows=[];limited=False;size=0
            if columns:
                for _ in range(201):
                    row=cursor.fetchone()
                    if row is None:break
                    values=[v if isinstance(v,(str,int,float,bool,type(None))) else str(v) for v in row]
                    import json
                    size+=len(json.dumps(values,ensure_ascii=False))
                    if len(rows)==200 or size>200000:limited=True;break
                    rows.append(values)
            affected=max(0,cursor.rowcount) if not columns else None
            if limited and item['kind']=='mysql':
                result=getattr(conn,'_result',None)
                if result is not None:result.unbuffered_active=False
                cursor._result=None;cursor.connection=None
                _close(entry)
                # MySQL writable connections use autocommit. Reconnect after abandoning an oversized result.
                try:connect(uid,workspace,ident)
                except ValueError:pass
            else:
                cursor.close();cursor=None;conn.commit()
            return {'columns':columns,'rows':rows,'limited':limited,'affected_rows':affected,'elapsed_ms':round((time.monotonic()-started)*1000),'connected':entry['conn'] is not None}
        except Exception as error:
            code=error.args[0] if error.args else None
            if code in (2003,2006,2013,2055,3024):
                if entry['conn'] is not None:
                    result=getattr(entry['conn'],'_result',None)
                    if result is not None:result.unbuffered_active=False
                if item['kind']=='mysql' and cursor is not None:
                    cursor._result=None;cursor.connection=None
                _close(entry)
            elif entry['conn'] is not None:
                try:entry['conn'].rollback()
                except Exception:_close(entry)
            reason={1045:'数据库账号认证失败',1142:'数据库账号没有执行该操作的权限',1143:'数据库账号没有所需字段权限',1290:'数据库服务拒绝写入（只读实例或账号限制）',1064:'SQL 语法错误',1146:'表不存在',2003:'无法连接数据库服务',2006:'连接已断开，执行结果未确认；请先核对数据库',2013:'连接中断，执行结果未确认；请先核对数据库，勿直接重复写入',3024:'SQL 执行超时'}.get(code)
            if isinstance(error,ValueError):raise
            raise ValueError(reason or ('SQL 执行失败：'+str(error) if item['kind']=='sqlite' else 'SQL 执行失败，请检查语法、账号权限与连接状态')) from None
        finally:
            if cursor is not None:
                try:cursor.close()
                except Exception:pass
            if entry['conn'] is not None and item['kind']=='sqlite':entry['conn'].set_progress_handler(None,0)


def touch(uid,workspace,ident):
    entry=_entry(uid,workspace,ident)
    with entry['lock']:
        if _alive(entry):entry['used']=time.monotonic()

def close_all():
    with _LOCK:entries=list(_HANDLES.values());_HANDLES.clear()
    for entry in entries:
        with entry['lock']:_close(entry)

import atexit
atexit.register(close_all)
