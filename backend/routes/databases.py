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
            if sid:
                if db.session_owner(sid)!=uid:return self._json({'error':'会话不存在'},404)
                workspace=project_directory(uid,sid)
                if body.get('workspace') and workspace and str(selected_directory(body['workspace']))!=str(workspace):return self._json({'error':'项目已切换，请重新打开数据库'},409)
                if db.session_archived(sid) and self.command!='GET':return self._json({'error':'归档会话只可查看'},409)
            else:
                requested=body.get('workspace') if body else (params.get('workspace') or [''])[0]
                workspace=selected_directory(requested) if requested else None
            if not workspace:return self._json({'error':'请先选择项目文件夹'},409)
            workspace=str(workspace)
            if self.command=='GET':return self._json({'workspace':workspace,'connections':databases.connections(uid,workspace)})
            action=body.get('action')
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
