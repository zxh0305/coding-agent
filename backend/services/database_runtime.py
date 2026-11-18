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


_UNCONFIRMED={2006,2013,2045,2055}  # connection lost while the statement may have run

def update(uid,workspace,ident,table,column,key,value):
    """Change one cell of one row: a single bound UPDATE, committed once.

    The workbench's SQL editor is never reused here, so a failed or interrupted
    edit is reported (and never silently retried) instead of duplicating a write.
    """
    plan=config._check(uid,workspace,ident,table,column,key,value)
    statement,parameters,kind=plan['statement'],plan['parameters'],plan['kind']
    conn=config._connect(plan['item'],writable=True)
    if kind=='sqlite':
        deadline=time.monotonic()+10;conn.set_progress_handler(lambda:time.monotonic()>deadline,1000)
    cursor=conn.cursor();executed=False
    try:
        cursor.execute(statement,parameters);executed=True
        affected=cursor.rowcount;cursor.close()
        if affected>1:
            conn.rollback();raise ValueError('定位条件匹配了多行，已取消修改；请改用主键定位')
        if kind=='sqlite':
            # Prove the new value really sits in a row with this key before committing
            # (rowcount is -1 for UPDATE in Python's sqlite3, so it proves nothing).
            check=conn.execute('SELECT 1 FROM '+config._quote_name('"',table)+' WHERE '+' AND '.join(config._quote_name('"',name)+(' IS NULL' if raw is None else ' IS ?') for name,raw in key.items())+
                               (' AND '+config._quote_name('"',column)+' IS NULL' if value is None else ' AND '+config._quote_name('"',column)+' IS ?'),
                               [raw for raw in key.values() if raw is not None]+([] if value is None else [value])).fetchone()
            if not check:
                conn.rollback();raise ValueError('没有匹配到这一行，可能已被删除或主键已变化；请刷新表数据后重试')
        elif affected==0:
            conn.rollback();raise ValueError('没有匹配到这一行，可能已被删除或主键已变化；请刷新表数据后重试')
        conn.commit()
        return {'ok':True,'affected_rows':affected,'sql':plan['sql']}
    except Exception as error:
        try:
            # SQLite keeps its statement in a transaction, so a rollback is exact.
            # MySQL autocommits: if the connection broke after execution the write
            # may or may not have landed, so report it as unconfirmed and never retry.
            if kind=='mysql' and executed:conn.close()
            else:conn.rollback()
        except Exception:pass
        if isinstance(error,ValueError):raise
        if kind=='sqlite':raise ValueError('修改失败：'+config._sqlite_reason(error)) from None
        code=error.args[0] if error.args else None
        if code in _UNCONFIRMED:raise ValueError('连接中断，修改结果未确认；请重新读取这一行核对，不要直接重复执行') from None
        reason={1045:'数据库账号认证失败',1054:'字段不存在，请刷新表数据后重试',1062:'该值与其他行重复，违反唯一约束',1064:'SQL 语法错误',
                1142:'数据库账号没有该表的修改权限',1143:'数据库账号没有该字段的修改权限',1264:'数值超出字段范围',
                1290:'数据库实例只读，无法写入',1406:'内容超出字段长度',3819:'该值未通过字段检查约束'}.get(code)
        raise ValueError(reason or '修改失败，请检查字段类型、约束和账号写权限') from None
    finally:
        if cursor is not None:
            try:cursor.close()
            except Exception:pass
        if kind=='sqlite' and conn is not None:
            try:conn.set_progress_handler(None,0);conn.close()
            except Exception:pass

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
