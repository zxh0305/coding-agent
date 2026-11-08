"""Project-bound read-only database connections. Credentials never leave this service."""
import json, os, re, sqlite3, threading, time, uuid
from pathlib import Path
import db
_LOCK=threading.RLock()

def _file():return db.DB_PATH.parent/'database-connections.json'
def _load():
    return json.loads(_file().read_text()) if _file().exists() else []
def _save(items):
    path=_file();path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as out:json.dump(items,out)
    os.replace(temp,path);os.chmod(path,0o600)
def connections(uid,workspace):
    workspace=str(Path(workspace).expanduser().resolve())
    with _LOCK:return [{k:v for k,v in x.items() if k!='password'} for x in _load() if x['owner']==uid and x['workspace']==workspace]
def save(uid,workspace,body):
    workspace=str(Path(workspace).expanduser().resolve())
    kind=body.get('kind');name=body.get('name','').strip()
    if kind not in ('sqlite','mysql') or not name or len(name)>100:raise ValueError('连接名称或类型无效')
    item=dict(id=uuid.uuid4().hex,owner=uid,workspace=workspace,name=name,kind=kind)
    if kind=='sqlite':
        path=Path(body.get('path','')).expanduser()
        if not path.is_absolute():path=Path(workspace)/path
        path=path.resolve()
        if not path.is_file():raise ValueError('SQLite 文件不存在，不会创建新库')
        if path==db.DB_PATH.resolve():raise ValueError('不能连接应用自己的数据库')
        item['path']=str(path)
    else:
        for key in ('host','user','database'):
            value=body.get(key)
            if not isinstance(value,str) or not value or len(value)>255:raise ValueError('MySQL 连接信息不完整')
            item[key]=value
        item['port']=int(body.get('port',3306));item['password']=body.get('password','')
        if not isinstance(item['password'],str) or not 1<=item['port']<=65535:raise ValueError('连接信息无效')
    with _LOCK:
        items=_load();items.append(item);_save(items)
    return item['id']
def remove(uid,workspace,ident):
    workspace=str(Path(workspace).expanduser().resolve())
    with _LOCK:_save([x for x in _load() if not(x['id']==ident and x['owner']==uid and x['workspace']==workspace)])
def _get(uid,workspace,ident):
    workspace=str(Path(workspace).expanduser().resolve())
    with _LOCK:
        for x in _load():
            if x['id']==ident and x['owner']==uid and x['workspace']==workspace:return x
    raise ValueError('连接不存在或不属于当前项目')
def _connect(item):
    if item['kind']=='sqlite':
        conn=sqlite3.connect(Path(item['path']).as_uri()+'?mode=ro',uri=True,timeout=3)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,200000)
        conn.execute('PRAGMA query_only=ON');deadline=time.monotonic()+5
        conn.set_progress_handler(lambda:time.monotonic()>deadline,1000)
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        conn.set_authorizer(lambda action,*args:sqlite3.SQLITE_OK if action in allowed and not(action==sqlite3.SQLITE_FUNCTION and str(args[1]).lower() in ('load_extension','writefile','readfile')) else sqlite3.SQLITE_DENY)
        return conn
    try:import pymysql
    except ImportError:raise ValueError('MySQL 需要安装可选依赖：pip install pymysql') from None
    conn=pymysql.connect(host=item['host'],port=item['port'],user=item['user'],password=item['password'],database=item['database'],connect_timeout=3,read_timeout=6,write_timeout=3,autocommit=False,cursorclass=pymysql.cursors.SSCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute('SET SESSION MAX_EXECUTION_TIME=5000')
            cursor.execute('START TRANSACTION READ ONLY')
    except Exception:conn.close();raise
    return conn

def query(uid,workspace,ident,sql):
    item=_get(uid,workspace,ident)
    if not isinstance(sql,str) or len(sql)>32000 or not re.match(r'^\s*(SELECT|WITH|EXPLAIN)\b',sql,re.I):raise ValueError('第一版仅支持 SELECT、WITH 查询和 EXPLAIN')
    if item['kind']=='mysql' and re.search(r';|\b(INTO|OUTFILE|DUMPFILE|SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK)\b',sql,re.I):raise ValueError('不支持多语句或带副作用的查询')
    conn=None
    try:
        conn=_connect(item);cursor=conn.cursor();cursor.execute(sql)
        cols=[x[0] for x in cursor.description or []];rows=[];size=0;limited=False
        for _ in range(201):
            row=cursor.fetchone()
            if row is None:break
            values=[value if isinstance(value,(str,int,float,bool,type(None))) else str(value) for value in row]
            size+=len(json.dumps(values,ensure_ascii=False))
            if len(rows)>=200 or size>200000:limited=True;break
            rows.append(values)
        return {'columns':cols,'rows':rows,'limited':limited}
    except ValueError:raise
    except Exception:raise ValueError('连接或查询失败：请检查连接信息、只读权限、SQL 语法及超时限制') from None
    finally:
        if conn:conn.close()
def tables(uid,workspace,ident):
    item=_get(uid,workspace,ident)
    sql="SELECT name,type,sql FROM sqlite_schema WHERE type IN ('table','view','index') AND name NOT LIKE 'sqlite_%' ORDER BY name" if item['kind']=='sqlite' else 'SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION'
    return query(uid,workspace,ident,sql)
