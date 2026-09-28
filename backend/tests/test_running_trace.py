"""
历史接口「进行中快照」的回归测试（cd backend && python3 -m unittest tests.test_running_trace -v）
================================================================================

动机：95a0572 给历史分页接口加了 running 分支（会话在跑时附带 running_trace +
running_started_at），落地当天连出两个崩点——
  1. `session_state` 少写下划线（真名 `_session_state`）：NameError，所有会话拉历史必崩；
  2. `round_started_at` 是 @property 却被加括号调用：TypeError，切回 running 会话必崩。
而 do_GET 原先没有 do_POST 那样的 try/except 兜底，异常一路抛到 socketserver——
socket 被直接关掉、不回任何 HTTP 响应，前端 fetch 只能报"无法连接后端服务"，把
代码 bug 伪装成服务没启动（截图事故）。

本文件起真实 HTTP 服务复现「回合进行中切回会话」的现场，钉死三件事：
  1. running 会话拉历史：200 + running_trace/running_started_at 形状正确；
  2. 空闲会话拉历史：200 且不带 running_trace（正常路径零额外字段）；
  3. GET 接口内部异常：500 JSON 兜底，而不是断连（URLError）。

全部用例跑在临时库里，不碰项目根的 agent_data.db；bus 用完即从 app._buses
摘除，避免同进程其他用例看到"幽灵 running"。
"""

import json
import time
import unittest
import urllib.error
from unittest.mock import patch

import app
import db
from tests.test_session_model import ApiTestBase

SNAP = [{"type": "round", "round": 1},
        {"type": "reasoning", "round": 1, "text": "思考中……"}]


class RunningTraceTest(ApiTestBase):
    """复用 test_session_model 的 ApiTestBase：真实服务 + 随机端口 + Bearer。"""

    def setUp(self):
        super().setUp()
        # bus 挂在 app._buses 这个模块级字典上，用完必须摘除，否则同进程
        # 后跑的其他测试文件里 s1 永远是 running
        self.addCleanup(app._buses.pop, "s1", None)

    def _go_running(self) -> str:
        """造「回合进行中」现场：一条用户消息 + turn_start + 按 mid 落快照。"""
        hist = [{"role": "user", "content": "帮我看看这个报错"}]
        db.save_messages("s1", hist, {})
        user_mid = hist[0]["_mid"]
        bus = app._event_bus("s1")
        bus.publish({"type": "turn_start", "started_at": time.time()})
        db.set_trace("s1", user_mid, json.dumps(SNAP, ensure_ascii=False))
        return user_mid

    def test_running_session_history_carries_snapshot(self):
        """核心回归：running 会话拉历史 200，快照与起点随响应带回。

        两个历史崩点在这条用例里都会现形：名字错 → 500/NameError；
        property 加括号 → （无兜底时代）断连 / （有兜底后）500 TypeError。
        """
        self._go_running()
        status, body = self.call("GET", "/api/sessions/s1/messages")
        self.assertEqual(status, 200)
        self.assertEqual(body["running_trace"], SNAP)
        self.assertIsInstance(body["running_started_at"], float)
        # 常规字段照常带回：快照分支不是替换而是增补
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")

    def test_idle_session_history_has_no_running_fields(self):
        """空闲会话（无 bus）：走正常路径，响应里不出现 running 字段。"""
        hist = [{"role": "user", "content": "普通提问"}]
        db.save_messages("s1", hist, {})
        status, body = self.call("GET", "/api/sessions/s1/messages")
        self.assertEqual(status, 200)
        self.assertNotIn("running_trace", body)
        self.assertNotIn("running_started_at", body)
        self.assertEqual(len(body["messages"]), 1)

    def test_get_exception_returns_500_json_not_disconnect(self):
        """do_GET 兜底回归：接口内部异常回 500 JSON，而非直接断连。

        无兜底时异常抛到 socketserver，连接被掐断，urllib 抛 URLError
        （前端即"无法连接后端服务"）；有兜底时同类异常变成结构化的 500。
        """
        def _boom(sid):
            raise RuntimeError("模拟接口内部错误")
        # call() 会把 HTTPError 吞成 (code, body) 元组；断连（无兜底时的旧行为）
        # 则以 URLError 直接从 call() 里炸出来——两种表现用这条断言即可区分
        with patch.object(db, "user_message_index", _boom):
            status, body = self.call("GET", "/api/sessions/s1/messages")
        self.assertEqual(status, 500)
        self.assertIn("服务器内部错误", body.get("error", ""))


if __name__ == "__main__":
    unittest.main()
