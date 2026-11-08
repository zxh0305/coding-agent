"""Local CLI sharing the application connection registry and SQL executors."""
import argparse,json
from services import databases
p=argparse.ArgumentParser(description='项目数据库查询与执行（execute 按账号权限提交修改）')
p.add_argument('action',choices=['list','tables','query','execute']);identity=p.add_mutually_exclusive_group(required=True);identity.add_argument('--owner',type=int,help='本机应用用户 ID');identity.add_argument('--user',help='本机应用用户名');p.add_argument('--workspace',required=True);p.add_argument('--connection');p.add_argument('--sql');p.add_argument('--file')
def main():
    from pathlib import Path
    args=p.parse_args();workspace=str(Path(args.workspace).expanduser().resolve())
    try:
        if args.user:
            import db
            user=db.get_user_by_name(args.user)
            if not user:raise ValueError('应用用户不存在')
            args.owner=user['id']
        if args.action=='list':result=databases.connections(args.owner,workspace)
        elif args.action=='tables':result=databases.tables(args.owner,workspace,args.connection)
        else:
            if bool(args.sql)==bool(args.file):p.error('请指定 --sql 或 --file 中的一项')
            sql=Path(args.file).read_text() if args.file else args.sql
            if args.action=='execute':
                from services.database_runtime import execute
                result=execute(args.owner,workspace,args.connection,sql)
            else:result=databases.query(args.owner,workspace,args.connection,sql)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except ValueError as error:p.exit(1,str(error)+'\n')
if __name__=='__main__':main()
