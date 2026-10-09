"""
任务列表接口回归测试（cd backend && python3 -m unittest tests.test_session_list -v）
==================================================================================

动机：ed96960 把 app.py 拆成 services/routes 装配层时，dispatch.py 在 import 期
写了 `_session_state = busref._session_state`。busref 的注入点在 import 时还是
None（app.install_service_hooks() 在其后才赋值），于是这个模块级别名把 None 永久
钉死在 dispatch 里——`GET /api/sessions` 每次都 TypeError → 500 JSON，前端拿不到
列表，整个任务栏空掉（界面表现为"数据没了"，库里 32 个会话一个没少）。

busref 的模块 docstring 早已写明"绝不可以在 import 期取值"，但没有任何测试
盯着这条约束：既有用例只覆盖 /api/sessions/<sid>/messages，没人调过列表接口。
本文件补两件事：
  1. 列表接口本身的行为（行、state、preview、归档分区）；
  2. 一条通用的"注入点没被 import 期快照"防线——扫 routes/* 的模块属性，
     任何与 busref 注入点同名且为 None 的模块级全局都算复发。

全部用例跑在临时库里，不碰项目根的 agent_data.db。
"""

import importlib
import unittest

import app
import db
from routes import busref
from tests.test_session_model import ApiTestBase


class SessionListApiTest(ApiTestBase):
    """GET /api/sessions 的真实 HTTP 行为（复用 ApiTestBase 的临时库 + 随机端口）。"""

    def test_list_returns_rows_with_state_and_preview(self):
        """核心回归：列表 200 且带 state / preview（修复前是 500 TypeError）。"""
        status, rows = self.call("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["id"] for r in rows), ["s1", "s2"])
        for r in rows:
            self.assertEqual(r["state"], "none")   # 从没跑过 → 不显示状态点
            self.assertIn("preview", r)
            self.assertIn("archived", r)

    def test_list_state_reflects_running_turn(self):
        """state 取的是装配层当前注入的实现（别再被 import 期快照成 None）。"""
        self.addCleanup(app._buses.pop, "s1", None)
        app._event_bus("s1").publish({"type": "turn_start"})
        _, rows = self.call("GET", "/api/sessions")
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["s1"]["state"], "running")
        self.assertEqual(by_id["s2"]["state"], "none")   # 兄弟会话不受影响

    def test_list_archived_partitions_sessions(self):
        """?archived=1 只看归档区，默认只看任务栏——两边都带同一套字段。"""
        db.archive_session("s2", 1)
        _, active = self.call("GET", "/api/sessions")
        self.assertEqual([r["id"] for r in active], ["s1"])
        _, archived = self.call("GET", "/api/sessions?archived=1")
        self.assertEqual([r["id"] for r in archived], ["s2"])
        self.assertEqual(archived[0]["state"], "none")
        self.assertIn("preview", archived[0])


class InjectedHooksNotSnapshottedTest(unittest.TestCase):
    """装配层注入点只能经 busref 属性访问，不许在 import 期取别名。

    import 期取值会拿到 None（注入发生在 import 之后），且此后永不更新——
    dispatch.py 的 `_session_state` 就是这么把任务列表打崩的。
    """

    def test_routes_modules_hold_no_import_time_none_copy(self):
        injected = [k for k, v in vars(busref).items()
                    if k not in ("log", "db") and not k.startswith("__")
                    and v is not None]
        self.assertIn("_session_state", injected)   # 注入确实发生了（否则本测试空转）
        for name in ("dispatch", "sessions", "chat", "models", "workspace", "git", "base"):
            mod = importlib.import_module(f"routes.{name}")
            for hook in injected:
                if hasattr(mod, hook):
                    self.assertIsNotNone(
                        getattr(mod, hook),
                        f"routes.{name}.{hook} 是 import 期快照的 None——"
                        f"请改成在调用点写 busref.{hook}")


if __name__ == "__main__":
    unittest.main()
