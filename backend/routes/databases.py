import db
from services import databases
class DatabaseRoutes:
    def _handle_databases(self):
        try:
            body=self._body() if self.command=='POST' else {}
            sid=body.get('session_id') if body else (self._query().get('session_id') or [''])[0]
            if db.session_owner(sid)!=self.user['id']:return self._json({'error':'会话不存在'},404)
            workspace=db.get_session_workspace(sid)
            if not workspace:return self._json({'error':'请先为会话选择项目'},409)
            uid=self.user['id'];action=body.get('action')
            if self.command=='GET':return self._json({'connections':databases.connections(uid,workspace)})
            if db.session_archived(sid):return self._json({'error':'归档会话只可查看'},409)
            if action=='save':return self._json({'id':databases.save(uid,workspace,body)})
            if action=='remove':databases.remove(uid,workspace,body.get('id'));return self._json({'ok':True})
            if action=='test':return self._json(databases.test_connection(uid,workspace,body.get('id')))
            if action=='query':return self._json(databases.query(uid,workspace,body.get('id'),body.get('sql')))
            if action=='tables':return self._json(databases.tables(uid,workspace,body.get('id')))
            raise ValueError('未知数据库操作')
        except ValueError as error:return self._json({'error':str(error)},400)
        except TypeError:return self._json({'error':'数据库参数格式无效'},400)
