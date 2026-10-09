"""工作区路由：切换工作区 / 目录浏览
=================================

从原 app.py 的 Handler 逐字搬移：_handle_workspace_set / _handle_fs_dirs。
"""

from pathlib import Path

import db

from routes import busref


class WorkspaceRoutes:
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
        target = Path(path).expanduser() if path else None
        if target is None or not target.is_dir():
            return self._json({"error": "目录不存在或不可用"}, 400)
        target = target.resolve()
        if target == Path(target.root):  # Path 与 str 比较为 False，必须同类型比较——
            return self._json({"error": "不能选择文件系统根目录"}, 400)  # 此前这里用 target.root 裸比，从未拦住过
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
