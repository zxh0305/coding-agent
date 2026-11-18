"""Project-bound connection configuration and bounded database browsing."""
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
    workspace=str(Path(workspace).expanduser().resolve()) if workspace is not None else None
    with _LOCK:return [{k:v for k,v in x.items() if k!='password'} for x in _load() if x['owner']==uid and (workspace is None or x['workspace']==workspace)]
def save(uid,workspace,body):
    workspace=str(Path(workspace).expanduser().resolve())
    kind=body.get('kind');name=body.get('name','').strip()
    if kind not in ('sqlite','mysql') or not name or len(name)>100:raise ValueError('连接名称或类型无效')
    existing=_get(uid,workspace,body['id']) if body.get('id') else None
    item=dict(id=existing['id'] if existing else uuid.uuid4().hex,owner=uid,workspace=workspace,name=name,kind=kind)
    if kind=='sqlite':
        path=Path(body.get('path','')).expanduser()
        if not path.is_absolute():path=Path(workspace)/path
        path=path.resolve()
        if not path.is_file():raise ValueError('SQLite 文件不存在，不会创建新库')
        item['path']=str(path)
    else:
        for key in ('host','user','database'):
            value=body.get(key)
            if not isinstance(value,str) or not value or len(value)>255:raise ValueError('MySQL 连接信息不完整')
            item[key]=value
        item['port']=int(body.get('port',3306));item['password']=body.get('password','') or (existing.get('password','') if existing and existing['kind']=='mysql' else '')
        if not isinstance(item['password'],str) or not 1<=item['port']<=65535:raise ValueError('连接信息无效')
    with _LOCK:
        items=[x for x in _load() if x['id']!=item['id']];items.append(item);_save(items)
    if existing:
        from services.database_runtime import forget
        forget(uid,workspace,item['id'])
    return item['id']
def remove(uid,workspace,ident):
    workspace=str(Path(workspace).expanduser().resolve())
    with _LOCK:_save([x for x in _load() if not(x['id']==ident and x['owner']==uid and x['workspace']==workspace)])
    from services.database_runtime import forget
    forget(uid,workspace,ident)
def _get(uid,workspace,ident):
    workspace=str(Path(workspace).expanduser().resolve())
    with _LOCK:
        for x in _load():
            if x['id']==ident and x['owner']==uid and x['workspace']==workspace:return x
    raise ValueError('连接不存在或不属于当前项目')
def _connect(item,metadata=False,writable=False):
    if item['kind']=='sqlite':
        conn=sqlite3.connect(Path(item['path']).as_uri()+('?mode=rw' if writable else '?mode=ro'),uri=True,timeout=3,check_same_thread=not writable)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,200000)
        if writable:return conn
        conn.execute('PRAGMA query_only=ON');deadline=time.monotonic()+5
        conn.set_progress_handler(lambda:time.monotonic()>deadline,1000)
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        conn.set_authorizer(lambda action,*args:sqlite3.SQLITE_OK if (metadata and action==sqlite3.SQLITE_PRAGMA and str(args[0]).lower() in ('table_info','table_xinfo','index_list','index_info','index_xinfo')) or action in allowed and not(action==sqlite3.SQLITE_FUNCTION and str(args[1]).lower() in ('load_extension','writefile','readfile')) else sqlite3.SQLITE_DENY)
        return conn
    try:import pymysql
    except ImportError:raise ValueError('MySQL 驱动未安装，请运行 python start.py 或安装 requirements.txt') from None
    conn=pymysql.connect(host=item['host'],port=item['port'],user=item['user'],password=item['password'],database=item['database'],charset='utf8mb4',connect_timeout=3,read_timeout=6,write_timeout=3,autocommit=writable,cursorclass=pymysql.cursors.SSCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute('SET SESSION MAX_EXECUTION_TIME=5000')
            if not writable:cursor.execute('START TRANSACTION READ ONLY')
    except Exception:conn.close();raise
    return conn

def test_connection(uid,workspace,ident):
    result=query(uid,workspace,ident,'SELECT 1')
    return {'ok':True,'message':'连接成功，只读查询可用'}

_READ_REASONS={1045:'MySQL 认证失败，请检查用户名和密码',1049:'MySQL 数据库不存在',1142:'数据库账号没有所需读取权限',1146:'查询的表不存在',1064:'SQL 语法错误',1193:'数据库不支持查询超时设置，请使用支持 MAX_EXECUTION_TIME 的 MySQL',2003:'无法连接 MySQL，请检查地址、端口和服务状态',2013:'MySQL 连接中断或查询超时',3024:'查询超过执行时间限制'}

def _reason(kind,error):
    """User-facing reason for a driver error; None when nothing more specific is known."""
    if kind=='sqlite':
        text=str(error)
        if 'attempt to write a readonly database' in text:raise ValueError('SQLite 文件只读，无法写入')
        if 'no such column' in text:raise ValueError('字段不存在，请刷新后重试')
        return None
    code=error.args[0] if error.args else None
    return _READ_REASONS.get(code)

def query(uid,workspace,ident,sql,*,_params=None,_metadata=False):
    item=_get(uid,workspace,ident)
    if not isinstance(sql,str) or len(sql)>32000:raise ValueError('SQL 无效或超过长度限制')
    sql=sql.strip().removesuffix(';').rstrip()
    ordinary=re.match(r'^(SELECT|WITH|EXPLAIN)\b',sql,re.I)
    metadata=item['kind']=='mysql' and re.match(r'^(SHOW\s+(?:FULL\s+)?(?:TABLES|COLUMNS|INDEX|INDEXES|KEYS|CREATE\s+TABLE)\b|DESCRIBE\s+)',sql,re.I)
    if not ordinary and not metadata and not _metadata:raise ValueError('仅支持只读查询、EXPLAIN 和 MySQL 表结构查询')
    if not _metadata and item['kind']=='mysql' and re.search(r';|\b(INTO|OUTFILE|DUMPFILE|SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK)\b',sql,re.I):raise ValueError('不支持多语句或带副作用的查询')
    conn=None;cursor=None
    try:
        conn=_connect(item,metadata=_metadata);cursor=conn.cursor();cursor.execute(sql,_params) if _params is not None else cursor.execute(sql)
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
    except Exception as error:
        raise ValueError(_reason(item['kind'],error) or '连接或查询失败：请检查连接信息、只读权限、SQL 语法及超时限制') from None
    finally:
        if conn:
            # We deliberately abandon rows beyond the cap, rather than draining
            # an unbounded SSCursor. Disarm PyMySQL's destructor before closing
            # its one-shot socket (never pooled or reused).
            result=getattr(conn,'_result',None)
            if result is not None and getattr(result,'unbuffered_active',False):
                result.unbuffered_active=False
                if cursor is not None:
                    cursor._result=None
                    cursor.connection=None
            conn.close()
def tables(uid,workspace,ident):
    item=_get(uid,workspace,ident)
    sql="SELECT name,type,sql FROM sqlite_schema WHERE type IN ('table','view','index') AND name NOT LIKE 'sqlite_%' ORDER BY name" if item['kind']=='sqlite' else 'SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,COLUMN_DEFAULT,EXTRA,COLUMN_COMMENT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION'
    return query(uid,workspace,ident,sql)


def _page(value):
    if type(value) is not int or not 0<=value<=100000:raise ValueError('分页参数无效')
    return value

def catalog(uid,workspace,ident,page=0,search=''):
    page=_page(page);item=_get(uid,workspace,ident)
    if not isinstance(search,str) or len(search)>100:raise ValueError('搜索内容过长')
    if item['kind']=='sqlite':
        sql="SELECT name,type,'' FROM sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' AND name LIKE ? ORDER BY name LIMIT 101 OFFSET ?"
    else:
        sql="SELECT TABLE_NAME,TABLE_TYPE,TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME LIMIT 101 OFFSET %s"
    result=query(uid,workspace,ident,sql,_params=('%'+search+'%',page*100),_metadata=True)
    return {'database':item.get('database') or Path(item['path']).name,'items':[dict(name=r[0],type=r[1],comment=r[2]) for r in result['rows'][:100]],'page':page,'has_more':len(result['rows'])>100}

def _table(uid,workspace,ident,name):
    if not isinstance(name,str) or not name or len(name)>255:raise ValueError('表名称无效')
    item=_get(uid,workspace,ident)
    sql="SELECT name FROM sqlite_schema WHERE name=? AND type IN ('table','view')" if item['kind']=='sqlite' else 'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s'
    if not query(uid,workspace,ident,sql,_params=(name,),_metadata=True)['rows']:raise ValueError('表不存在或无权读取')
    quote='"' if item['kind']=='sqlite' else '`'
    return item,quote+name.replace(quote,quote*2)+quote

def table_schema(uid,workspace,ident,name):
    item,quoted=_table(uid,workspace,ident,name)
    if item['kind']=='sqlite':
        result=query(uid,workspace,ident,'PRAGMA table_info('+quoted+')',_metadata=True)
        return {'columns':['字段','类型','可空','默认值','主键'],'rows':[[r[1],r[2],'否' if r[3] or r[5] else '是',r[4],'是' if r[5] else ''] for r in result['rows']],'limited':result['limited']}
    return query(uid,workspace,ident,"SELECT COLUMN_NAME AS `字段`,COLUMN_TYPE AS `类型`,IS_NULLABLE AS `可空`,COLUMN_DEFAULT AS `默认值`,COLUMN_KEY AS `键`,EXTRA AS `属性`,COLUMN_COMMENT AS `说明` FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",_params=(name,),_metadata=True)

def table_rows(uid,workspace,ident,name,page=0):
    page=_page(page);item,quoted=_table(uid,workspace,ident,name)
    if item['kind']=='sqlite':
        info=query(uid,workspace,ident,'PRAGMA table_info('+quoted+')',_metadata=True)
        keys=[r[1] for r in sorted(info['rows'],key=lambda r:r[5]) if r[5]]
        quote='"'
    else:
        info=query(uid,workspace,ident,"SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION",_params=(name,),_metadata=True)
        keys=[r[0] for r in info['rows']];quote='`'
    order=' ORDER BY '+','.join(quote+k.replace(quote,quote*2)+quote for k in keys) if keys else ''
    result=query(uid,workspace,ident,f'SELECT * FROM {quoted}{order} LIMIT 101 OFFSET {page*100}',_metadata=True)
    result['has_more']=len(result['rows'])>100;result['rows']=result['rows'][:100];result['page']=page;result['stable_order']=bool(keys)
    return result


def rename(uid,workspace,ident,name):
    if not isinstance(name,str) or not name.strip() or len(name)>100:raise ValueError('请输入连接名称（最多100字）')
    with _LOCK:
        item=_get(uid,workspace,ident);items=_load()
        for entry in items:
            if entry['id']==item['id']:entry['name']=name.strip()
        _save(items)
    return {'ok':True}


# --- Single-cell editing --------------------------------------------------------
# A cell edit changes one column of one row: one bound UPDATE, committed once and
# never retried. The same UPDATE is rendered with literals so the user sees what
# changed and can hand it to ops. Row identity comes from the grid; the server
# re-checks that it resolves to exactly one row that still exists.
_SQLITE_TEXT_TYPE=re.compile(r'^(?:TEXT|CHAR|CLOB|VARCHAR|NCHAR|NVARCHAR|BLOB\b(?!\s*\(\s*0\s*\)))',re.I)
_SQLITE_NUMERIC_TYPE=re.compile(r'INT|REAL|FLOA|DOUB|NUM|DEC|BOOL',re.I)
_MYSQL_TEXT_TYPE=re.compile(r'CHAR|TEXT|BLOB|ENUM|SET|JSON|BINARY|GEOMETRY')
_MYSQL_DATE_TYPE=re.compile(r'DATE|TIME|YEAR')
_MYSQL_INTEGER_TYPE=re.compile(r'^(?:TINYINT|SMALLINT|MEDIUMINT|INT(?:EGER)?\b|BIGINT|YEAR|BIT|BOOL)')
_MYSQL_NUMBER=re.compile(r'-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')

def _literal(text):
    return "'"+text.replace("'","''")+"'"

def _quote_name(quote,name):
    return quote+name.replace(quote,quote*2)+quote

def _literal_value(kind,text,column_type):
    """Render a value as a SQL literal that keeps the column's stored type."""
    if text is None:return 'NULL'
    if kind=='sqlite':
        untyped=_SQLITE_TEXT_TYPE.match(column_type or '') or not _SQLITE_NUMERIC_TYPE.search(column_type or '')
        if untyped:return _literal(text)
        try:float(text)
        except ValueError:return _literal(text)
        return text
    if _MYSQL_DATE_TYPE.search(column_type):return _literal(text)
    if _MYSQL_INTEGER_TYPE.match(column_type) and re.fullmatch(r'-?\d+',text):return text
    if not _MYSQL_TEXT_TYPE.search(column_type) and _MYSQL_NUMBER.fullmatch(text):return text
    return _literal(text)

def _sqlite_reason(error):
    """Translate the SQLite errors a cell edit can hit into what the user must do."""
    text=str(error)
    if 'readonly database' in text or 'read-only' in text:return 'SQLite 文件或目录只读，数据库账号无法写入'
    if 'no such column' in text:return '字段不存在，请刷新表数据后重试'
    if 'no such table' in text:return '表不存在，请刷新后重试'
    if 'locked' in text or 'busy' in text:return '数据库正被其他程序写入，请稍后重试'
    if 'UNIQUE constraint failed' in text:return '该值与其他行重复，违反唯一约束'
    if 'NOT NULL constraint failed' in text:return '该字段不允许为空'
    if 'CHECK constraint failed' in text:return '该值未通过字段检查约束'
    return '请检查字段类型、约束和账号写权限'

def _columns(conn,kind,table):
    """Field name -> declared type, or raise when the table is not readable."""
    if kind=='sqlite':
        temporary=[row[0] for row in conn.execute("SELECT name FROM sqlite_temp_master WHERE type IN ('table','view') AND name=?",(table,))]
        if not temporary and not conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name=?",(table,)).fetchall():raise ValueError('表不存在或无权读取')
        return {row[1] or '':row[2] for row in conn.execute('PRAGMA '+(('temp.' if temporary else '')+'table_xinfo(')+_literal(table)+')')}
    with conn.cursor() as cursor:
        cursor.execute("SELECT COLUMN_NAME,COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",(table,))
        types={row[0]:row[1] or '' for row in cursor.fetchall()}
    if not types:raise ValueError('表不存在或无权读取')
    return types

_BAD_DEFAULT=re.compile(r'^\s*(?:CURRENT_TIMESTAMP|CURRENT_TIMESTAMP\(\)|NOW\(\)|LOCALTIME|LOCALTIMESTAMP)\s*$',re.I)

def _check(uid,workspace,ident,table,column,key,value):
    """Validate a cell edit without writing; returns the statement and its literals."""
    for name,label in ((table,'表名称'),(column,'字段名称')):
        if not isinstance(name,str) or not name or len(name)>255:raise ValueError(label+'无效')
    if value is not None and not isinstance(value,str):raise ValueError('单元格内容无效')
    if value is not None and len(value)>200000:raise ValueError('内容过长，最多 200000 字符')
    if not isinstance(key,dict) or not key or len(key)>32:raise ValueError('缺少主键或唯一值，无法定位这一行')
    for name,raw in key.items():
        if not isinstance(name,str) or not name or len(name)>255:raise ValueError('定位字段无效')
        if raw is not None and not isinstance(raw,str):raise ValueError('定位字段值无效')
    item=_get(uid,workspace,ident);kind=item['kind']
    if kind=='sqlite' and Path(item['path']).resolve()==Path(db.DB_PATH.resolve()):raise ValueError('应用自身的数据库请在应用里修改，工作台不提供直接改表')
    conn=_connect(item,metadata=True)
    try:types=_columns(conn,kind,table)
    finally:conn.close()
    for name in (column,*key):
        if name not in types:raise ValueError('字段不存在或无权读取，请刷新表数据后重试')
    quote='"' if kind=='sqlite' else '`'
    table_sql=_quote_name(quote,table)
    if kind=='sqlite':
        # NULL keys become a literal "IS NULL": a bound NULL in "IS ?" is treated as
        # a one-shot flag by SQLite's statement planner and would only match once.
        where=' AND '.join(_quote_name(quote,name)+(' IS NULL' if raw is None else ' IS ?') for name,raw in key.items())
        statement=f'UPDATE {table_sql} SET {_quote_name(quote,column)} = ? WHERE {where}'
        parameters=[value]+[raw for raw in key.values() if raw is not None]
    else:
        statement=f'UPDATE {table_sql} SET {_quote_name(quote,column)} = %s WHERE '+' AND '.join(_quote_name(quote,name)+' = %s' for name in key)
        parameters=[value]+list(key.values())
    literal_where=' AND '.join(_quote_name(quote,name)+' IS NULL' if raw is None else _quote_name(quote,name)+' = '+_literal_value(kind,raw,types[name]) for name,raw in key.items())
    preview=f'UPDATE {table_sql} SET {_quote_name(quote,column)} = {_literal_value(kind,value,types[column])} WHERE {literal_where}'
    return {'item':item,'kind':kind,'statement':statement,'parameters':parameters,
            'sql':preview,'table':table,'column':column,'key':dict(key),'value':value}

def table_columns(uid,workspace,ident,table):
    """Field names of one table, for SQL console completion; one small query per table."""
    if not isinstance(table,str) or not table or len(table)>255:raise ValueError('表名称无效')
    sql="SELECT name FROM pragma_table_xinfo(?)" if _get(uid,workspace,ident)['kind']=='sqlite' else "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION"
    result=query(uid,workspace,ident,sql,_params=(table,),_metadata=True)
    return {'table':table,'columns':[row[0] for row in result['rows'] if row[0]]}

def edit_identity(uid,workspace,ident,table):
    """How the grid may identify a row for editing (primary key preferred)."""
    if not isinstance(table,str) or not table or len(table)>255:raise ValueError('表名称无效')
    item=_get(uid,workspace,ident);kind=item['kind']
    if kind=='sqlite' and Path(item['path']).resolve()==Path(db.DB_PATH.resolve()):
        return {'editable':False,'primary':[],'unique':[],'reason':'应用自身的数据库请在应用里修改，工作台不提供直接改表'}
    conn=_connect(item,metadata=True)
    try:
        if kind=='sqlite':
            info=conn.execute('PRAGMA table_xinfo('+_literal(table)+')').fetchall()
            if not info:raise ValueError('表不存在或无权读取')
            primary=[row[1] for row in info if row[5] and row[6] in (0,1)]
            if primary:return {'editable':True,'primary':primary,'unique':[],'reason':''}
            # No integer rowid access here (views and WITHOUT ROWID tables): accept a unique index.
            unique=[]
            for row in conn.execute('PRAGMA index_list('+_literal(table)+')').fetchall():
                if not row[2]:continue
                parts=conn.execute('PRAGMA index_xinfo('+_literal(row[1])+')').fetchall()
                keys=[(part[2],part[4]) for part in sorted(parts,key=lambda part:part[0]) if part[5]]
                if keys and all(name not in (None,'rowid','_rowid_','oid') for name,_ in keys):unique.append([name for name,_ in keys])
            return {'editable':bool(unique),'primary':[],'unique':unique[:3],'reason':'' if unique else '没有主键，也没有可用于定位的唯一索引，无法安全地只改一行'}
        with conn.cursor() as cursor:
            cursor.execute("SELECT COLUMN_NAME,IS_NULLABLE,DATA_TYPE,COLUMN_DEFAULT,EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",(table,))
            columns=cursor.fetchall()
            if not columns:raise ValueError('表不存在或无权读取')
            nullable={row[0]:(row[1]=='YES') for row in columns}
            default={row[0]:row[3] or '' for row in columns}
            auto={row[0]:(re.search(r'auto_increment|generated',row[4] or '',re.I) is not None) for row in columns}
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION",(table,))
            primary=[row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT INDEX_NAME,COLUMN_NAME,NON_UNIQUE FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND NON_UNIQUE=0 AND INDEX_NAME<>'PRIMARY' ORDER BY INDEX_NAME,SEQ_IN_INDEX",(table,))
            groups=[]
            for name,column,non_unique in cursor.fetchall():
                if groups and groups[-1][0]==name:groups[-1][1].append(column)
                else:groups.append([name,[column]])
    finally:conn.close()
    if primary:return {'editable':True,'primary':primary,'unique':[],'reason':''}
    unique=[]
    for _,keys in groups:
        if all(name in auto for name in keys) and not any(auto[name] for name in keys):
            if all(not nullable[name] and not _BAD_DEFAULT.match(default[name]) for name in keys):unique.append(keys)
    return {'editable':bool(unique),'primary':[],'unique':unique[:3],'reason':'' if unique else '没有主键，也没有可用于定位的非空唯一索引，无法安全地只改一行'}

def update_preview(uid,workspace,ident,table,column,key,value):
    """The exact UPDATE a manual edit would run, for review and for handing to ops."""
    plan=_check(uid,workspace,ident,table,column,key,value)
    return {'table':table,'column':column,'sql':plan['sql'],'old_value':plan['key'].get(column),'new_value':value}
