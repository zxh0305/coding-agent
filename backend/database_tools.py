"""Read-only Agent access to existing project connections; no credentials in tool results."""
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
        elif action=='tables':result=databases.tables(uid,workspace,connection)
        else:result=databases.query(uid,workspace,connection,sql)
        return json.dumps({'ok':True,'result':result},ensure_ascii=False)
    except ValueError:return json.dumps({'ok':False,'error':'查询失败，请检查连接 ID、只读 SQL、权限和超时；不要尝试写入'})
def database_connections(ctx=None):return _run(ctx,'list')
def database_schema(connection,ctx=None):return _run(ctx,'tables',connection)
def database_query(connection,sql,ctx=None):return _run(ctx,'query',connection,sql)
REGISTRY={name:globals()[name] for name in ('database_connections','database_schema','database_query')}
SCHEMAS=[]
for name,description,properties,required in [
 ('database_connections','列出当前项目已配置的数据库连接，不返回密码。',{},[]),
 ('database_schema','读取当前项目数据库的表结构。',{'connection':{'type':'string'}},['connection']),
 ('database_query','执行只读 SQL，最多返回200行。禁止写入，先读取结构再查询。',{'connection':{'type':'string'},'sql':{'type':'string'}},['connection','sql'])]:
    SCHEMAS.append({'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required}}})
