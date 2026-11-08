"""Project-scoped Agent database tools; no connection credentials in tool results."""
import json

def _run(ctx,action,connection=None,sql=None):
    import db
    from services import databases
    sid=getattr(ctx,'session_id',None);uid=db.session_owner(sid)
    workspace=db.get_session_workspace(sid)
    if uid is None or not workspace or str(ctx.workspace)!=workspace:
        return json.dumps({'ok':False,'error':'数据库工具需要已绑定项目的有效会话'})
    try:
        if action=='list':result=[{k:v for k,v in x.items() if k in ('id','name','kind')} for x in databases.connections(uid,workspace)]
        elif action=='execute':
            from services.database_runtime import execute
            result=execute(uid,workspace,connection,sql)
        elif action=='tables':result=databases.tables(uid,workspace,connection)
        else:result=databases.query(uid,workspace,connection,sql)
        return json.dumps({'ok':True,'result':result},ensure_ascii=False)
    except ValueError as error:return json.dumps({'ok':False,'error':str(error)},ensure_ascii=False)
def database_connections(ctx=None):return _run(ctx,'list')
def database_schema(connection,ctx=None):return _run(ctx,'tables',connection)
def database_execute(connection,sql,ctx=None):return _run(ctx,'execute',connection,sql)
def database_query(connection,sql,ctx=None):return _run(ctx,'query',connection,sql)
REGISTRY={name:globals()[name] for name in ('database_connections','database_schema','database_query','database_execute')}
SCHEMAS=[]
for name,description,properties,required in [
 ('database_execute','执行用户要求的 SQL（可增删改及表结构操作），由数据库账号权限控制，每次一条语句并立即提交，返回影响行数。',{'connection':{'type':'string'},'sql':{'type':'string'}},['connection','sql']),
 ('database_connections','列出当前项目已配置的数据库连接，不返回密码。',{},[]),
 ('database_schema','读取当前项目数据库的表结构。',{'connection':{'type':'string'}},['connection']),
 ('database_query','执行只读 SQL，最多返回200行。禁止写入，先读取结构再查询。',{'connection':{'type':'string'},'sql':{'type':'string'}},['connection','sql'])]:
    SCHEMAS.append({'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required}}})
