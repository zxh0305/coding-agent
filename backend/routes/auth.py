"""鉴权路由：注册 / 登录 / 退出 / me
=================================

从原 app.py 的 Handler._handle_auth 逐字搬移。
"""

import re

import db


class AuthRoutes:
    def _handle_auth(self, action: str):
        """register / login / logout / me。token 存数据库，服务重启不掉线。"""
        if action == "me":  # GET 已单独处理，这里是其他动词的兜底
            user = self._auth_user()
            if user is None:
                return self._json({"error": "未登录"}, 401)
            return self._json({"username": user["username"]})
        body = self._body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if action == "logout":
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                db.delete_token(header[7:].strip())
            return self._json({"ok": True})
        if not re.fullmatch(r"[\w\u4e00-\u9fff.-]{1,24}", username):
            return self._json({"error": "用户名须为 1-24 位字母/数字/下划线/中文/点/横线"}, 400)
        if len(password) < 4 or len(password) > 64:
            return self._json({"error": "密码长度须为 4-64 位"}, 400)
        if action == "register":
            try:
                user = db.create_user(username, password)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            if db.count_users() == 1:  # 第一个注册的用户认领升级前的无主旧任务
                claimed = db.claim_orphan_sessions(user["id"])
                if claimed:
                    from routes import busref
                    busref.log.info("升级前 %d 个旧任务已划归首个用户 %s", claimed, username)
        elif action == "login":
            user = db.get_user_by_name(username)
            if user is None or not db.verify_password(password, user["password_hash"]):
                return self._json({"error": "用户名或密码错误"}, 401)
        else:
            return self._json({"error": "未知接口"}, 404)
        token = db.create_token(user["id"])
        from routes import busref
        busref.log.info("用户 %s %s成功", username, "注册并登录" if action == "register" else "登录")
        self._json({"token": token, "username": user["username"]})
