"""工作区路由：切换工作区 / 目录浏览
=================================

从原 app.py 的 Handler 逐字搬移：_handle_workspace_set / _handle_fs_dirs。
"""

from pathlib import Path

import db
from services.workspace import selected_directory

from routes import busref


class WorkspaceRoutes:
    def _handle_project_context(self):
        from services.project_context import ProjectContext
        from services.project_environment import discover, preference_key
        from services.workspace import project_directory
        from code_tools import DEFAULT_WORKSPACE
        query = self._query()
        sid = (query.get("session_id") or [""])[0]
        requested = (query.get("workspace") or [""])[0]
        uid = self.user["id"]
        if sid and db.session_owner(sid) != uid:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        try:
            target = (project_directory(uid, sid) or DEFAULT_WORKSPACE) if sid else selected_directory(requested)
            target = selected_directory(str(target))
            if sid and requested and str(target) != requested:
                return self._json({"error": "任务目录已变更，请重新打开"}, 409)
            key = preference_key(target, uid)
            if self.command == "POST":
                preferred = self._body().get("executable")
                profile = discover(target)
                paths = [c["path"] for c in profile["candidates"]]
                if preferred != "system" and preferred not in paths:
                    raise ValueError("请从当前项目的解释器候选中选择")
                db.set_setting(key, preferred)
            profile = discover(target, db.get_setting(key))
            return self._json({"rules": ProjectContext(target).refresh(), "environment": profile})
        except (ValueError, OSError, RuntimeError) as e:
            return self._json({"error": str(e)}, 400)

    def _handle_workspace_set(self):
        """切换工作区（按任务隔离，替代曾经的"全局环境变量 + 写回 .env"）：
        - 带 session_id：只改该任务的工作区（须是自己的任务）；
        - 不带：改当前用户的"新任务默认工作区"。
        正在生成的任务拒绝切换——生成中的 Agent 仍持有旧工作区，切了也要等
        下一条消息才生效，用户容易误以为切失败。
        """
        b = self._body()
        path = str(b.get("path") or "").strip()
        sid = str(b.get("session_id") or "").strip()
        try:
            target = selected_directory(path)
        except (ValueError, RuntimeError, OSError) as e:
            return self._json({"error": str(e)}, 400)
        if sid:
            if db.session_owner(sid) != self.user["id"]:
                return self._json({"error": "任务不存在或不属于当前用户"}, 404)
            with busref._lock:
                lock = busref._session_locks.get(sid)
                if lock is not None and lock.locked():
                    return self._json({"error": "该任务正在生成，请等回答结束再切换工作区"}, 409)
                busref._agents.pop(sid, None)  # 丢弃旧实例：下一条消息用新工作区重建（历史从库里恢复）
            db.set_session_workspace(sid, str(target))
            busref.log.info("[会话 %s] 工作区切换为 %s", sid, target)
        else:
            db.set_setting(f"default_workspace:{self.user['id']}", str(target))
            busref.log.info("用户 %s 的新任务默认工作区: %s", self.user["username"], target)
        self._json({"ok": True, "path": str(target), "scope": "session" if sid else "default"})

    def _handle_fs_dirs(self):
        """列出某个目录下的子目录，供前端"选择工作区"弹窗逐级浏览（起点：用户主目录）。"""
        qs = self._query()
        target = Path(qs.get("path", [str(Path.home())])[0]).expanduser()
        if not target.is_dir():
            return self._json({"error": "目录不存在"}, 400)
        dirs = sorted(
            (e.name for e in target.iterdir() if e.is_dir()),
            key=lambda n: n.startswith("."),  # 普通目录在前，隐藏目录靠后
        )
        parent = target.parent
        self._json({
            "path": str(target),
            "parent": str(parent) if parent != target else None,
            "home": str(Path.home()),
            "dirs": dirs,
        })
