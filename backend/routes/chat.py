"""对话控制路由：停止 / 权限确认 / 权限模式
=========================================

从原 app.py 的 Handler 逐字搬移：
  _handle_chat_stop / _handle_permission / _handle_perm_mode
"""

import db
import permissions

from routes import busref


class ChatRoutes:
    def _handle_chat_stop(self):
        """停止指定任务的生成：给 Agent 的停止开关置位。

        看护线程随即掐断 LLM 连接，agent.run 带着已生成的部分内容收尾，
        事件流上照常收到 done(stopped=true) + turn_end。这里刻意不拿全局锁——
        恰恰是生成卡住时，停止请求必须还能进来。
        """
        sid = str(self._body().get("session_id") or "")
        if not sid or db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        agent = busref._stop_target(sid)
        if agent is None or agent.cancel_event is None or agent.cancel_event.is_set():
            return self._json({"ok": True, "running": False})
        agent.stop()
        busref.log.info("[会话 %s] 用户请求停止生成", sid)
        self._json({"ok": True, "running": True})

    def _handle_permission(self, sid: str, pid: str):
        """权限确认的决定回令：{"decision": "allow"|"allow_session"|"deny"}。

        时刻注意：回合 worker 此刻正阻塞在闸门的等待上（permission_request
        已发出），本 handler 跑在 HTTP 线程、只调 agent.resolve_permission 在
        闸门上 set 一个 Event 唤醒它——不持会话锁、不碰消息历史。等待超过
        PERMISSION_ASK_TIMEOUT 无人应答会按拒绝收场，此后迟到的点击在这里
        得到 ok=false（前端提示"确认已失效"）。会话实例被重建（切模型/工作区）
        后 pending 随旧实例丢弃，同样落到 ok=false——等待方超时兜底，无悬挂。"""
        decision = str(self._body().get("decision") or "")
        agent = busref._agents.get(sid)
        if agent is None:
            return self._json({"ok": False, "error": "任务不存在或已重建，确认已失效"}, 404)
        if decision not in ("allow", "allow_session", "deny"):
            return self._json({"error": "decision 须为 allow / allow_session / deny"}, 400)
        ok = agent.resolve_permission(pid, decision)
        if not ok:
            return self._json({"ok": False, "error": "确认请求不存在或已被处理"}, 404)
        self._json({"ok": True})

    def _handle_perm_mode(self, sid: str):
        """读/写会话的权限模式（readonly|confirm|yolo）。按工作区记忆：
        同一项目文件夹的新任务沿用上次选择的模式（与用户规则的隔离粒度一致）；
        无工作区的会话落在空 key 下（等于全局默认）。闸门每次判定经 mode_loader
        现读，切换后下一轮工具调用立即生效，无需重建会话。"""
        ws = db.get_session_workspace(sid) or ""
        if self.command == "POST":
            mode = str(self._body().get("mode") or "").strip()
            if mode not in permissions.MODES:
                return self._json({"error": f"mode 须为 {'/'.join(permissions.MODES)}"}, 400)
            db.set_setting(f"perm_mode:{ws}", mode)
            busref.log.info("[会话 %s] 权限模式切换为 %s（工作区 %s）", sid, mode, ws or "无")
        self._json({"mode": db.get_setting(f"perm_mode:{ws}", "confirm")})
