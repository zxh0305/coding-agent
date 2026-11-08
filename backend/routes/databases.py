import db
from services import databases
class DatabaseRoutes:
    def _handle_databases(self):
        try:
            body=self._body() if self.command=='POST' else {}
            params=self._query() if self.command=='GET' else {}
            sid=body.get('session_id') if body else (params.get('session_id') or [''])[0]
            uid=self.user['id']
            from services.workspace import selected_directory,project_directory
            global_scope=body.get('global') is True or (params.get('global') or [''])[0]=='1'
            if global_scope:
                workspace=None
            elif sid:
                if db.session_owner(sid)!=uid:return self._json({'error':'会话不存在'},404)
                workspace=project_directory(uid,sid)
                if body.get('workspace') and workspace and str(selected_directory(body['workspace']))!=str(workspace):return self._json({'error':'项目已切换，请重新打开数据库'},409)
                if db.session_archived(sid) and self.command!='GET':return self._json({'error':'归档会话只可查看'},409)
            else:
                requested=body.get('workspace') if body else (params.get('workspace') or [''])[0]
                workspace=selected_directory(requested) if requested else None
            if not workspace and not global_scope:return self._json({'error':'请先选择项目文件夹'},409)
            workspace=str(workspace) if workspace is not None else None
            if self.command=='GET':return self._json({'workspace':workspace,'connections':databases.connections(uid,workspace)})
            action=body.get('action')
            from services import database_runtime as runtime,database_snippets as snippets
            if global_scope:
                owned=databases.connections(uid,None)
                if action=='status':return self._json({'statuses':{x['id']:runtime.status(uid,x['workspace'],x['id']) for x in owned}})
                existing=next((x for x in owned if x['id']==body.get('id')),None)
                if action=='save' and not body.get('id'):
                    from pathlib import Path
                    if body.get('kind')=='sqlite' and not Path(body.get('path','')).expanduser().is_absolute():raise ValueError('独立连接请填写 SQLite 文件的绝对路径')
                    workspace=str(db.DB_PATH.parent.resolve())
                elif existing:workspace=existing['workspace']
                else:raise ValueError('请选择数据库连接')
            if action=='rename':return self._json(databases.rename(uid,workspace,body.get('id'),body.get('name')))
            if action=='connect':return self._json(runtime.connect(uid,workspace,body.get('id')))
            if action=='disconnect':return self._json(runtime.disconnect(uid,workspace,body.get('id')))
            if action=='status':return self._json({'statuses':{item['id']:runtime.status(uid,workspace,item['id']) for item in databases.connections(uid,workspace)}})
            if action=='execute':return self._json(runtime.execute(uid,workspace,body.get('id'),body.get('sql')))
            if action=='snippets':return self._json({'items':snippets.list_snippets(uid,workspace,body.get('id') if global_scope else None)})
            if action=='snippet_save':return self._json({'id':snippets.save(uid,workspace,body,body.get('id') if global_scope else None)})
            if action=='snippet_remove':snippets.remove(uid,workspace,body.get('snippet_id'),body.get('id') if global_scope else None);return self._json({'ok':True})
            if action in ('catalog','schema','rows','query','tables'):runtime.touch(uid,workspace,body.get('id'))
            if action=='catalog':return self._json(databases.catalog(uid,workspace,body.get('id'),body.get('page',0),body.get('search','')))
            if action=='schema':return self._json(databases.table_schema(uid,workspace,body.get('id'),body.get('table')))
            if action=='rows':return self._json(databases.table_rows(uid,workspace,body.get('id'),body.get('table'),body.get('page',0)))
            if action=='save':return self._json({'id':databases.save(uid,workspace,body)})
            if action=='remove':databases.remove(uid,workspace,body.get('id'));return self._json({'ok':True})
            if action=='test':return self._json(databases.test_connection(uid,workspace,body.get('id')))
            if action=='query':return self._json(databases.query(uid,workspace,body.get('id'),body.get('sql')))
            if action=='tables':return self._json(databases.tables(uid,workspace,body.get('id')))
            raise ValueError('未知数据库操作')
        except ValueError as error:return self._json({'error':str(error)},400)
        except TypeError:return self._json({'error':'数据库参数格式无效'},400)
