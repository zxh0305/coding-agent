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
import threading
import unittest

import app
import db
import memory
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


class AssemblyInjectionCompletenessTest(unittest.TestCase):
    """装配完成后，全部声明的注入点必须非 None——缺失要在启动时炸。

    上一条测试管"import 期不许取别名"（拿到的是 None 且永不更新）；这一条管
    另一个方向："声明的注入点忘了赋值"。两者合起来才封住整类事故：
      * 忘赋值 → 占位恒为 None，直到请求走到那行才 500（本项目真实发生过：
        app.py 拆分后 GET /api/sessions 每次 500，前端任务栏整条空掉）；
      * 现在改为装配完成即刻校验（app._assert_hooks_installed），坏启动而不是
        坏用户页面。

    覆盖 services/*（模块属性直接注入，此前完全没有保护）。
    """

    def test_all_declared_hooks_are_installed(self):
        """真实装配下：services/* 与 busref 的注入点全部就位。"""
        app._assert_hooks_installed()   # 不抛 = 通过
        for mod, names in app._SERVICE_INJECTIONS.items():
            for n in names:
                self.assertIsNotNone(
                    getattr(mod, n, None),
                    f"{mod.__name__}.{n} 未注入——请检查 install_service_hooks()")

    def test_missing_service_hook_is_detected(self):
        """漏注入一个 services 属性：校验必须抛错（否则本测试空转）。"""
        target = app._svc_turn
        saved = target._spawn_title_generation
        target._spawn_title_generation = None
        try:
            with self.assertRaises(RuntimeError) as ctx:
                app._assert_hooks_installed()
            self.assertIn("_spawn_title_generation", str(ctx.exception))
        finally:
            target._spawn_title_generation = saved
        app._assert_hooks_installed()   # 复原后必须重新通过

    def test_missing_busref_hook_is_detected(self):
        """漏注入一个 busref 注入点：同样必须抛错。"""
        saved = busref._stop_target
        busref._stop_target = None
        try:
            with self.assertRaises(RuntimeError) as ctx:
                busref.assert_installed()
            self.assertIn("_stop_target", str(ctx.exception))
        finally:
            busref._stop_target = saved
        busref.assert_installed()

    def test_busref_rejects_unknown_injection_name(self):
        """注入了 busref 不认识的键：KeyError（防拼写错误静默丢注入）。"""
        with self.assertRaises(KeyError):
            busref.install(_no_such_hook=1)


class SessionDeleteMemoryCleanupTest(ApiTestBase):
    """删除会话必须把它在【每一个】以 sid 为键的内存 dict 里清掉。

    回归动机：这些 dict 都只增不减，而 _delete_session_cleanup 此前漏了
    _session_locks 与 _sigs。作者对锁表的注释是"数量级 = 任务数，可接受"，
    但那个假设只在"会话从不被删除"时成立——长期建/删会话会让锁表随【删除过
    的会话数】无界增长（一个 Lock 对象 + 一个指纹字符串一条，慢性但真实）。
    """

    def setUp(self):
        super().setUp()
        # 这些内存 dict 是【进程级单例】，测试之间会互相污染：本类写入后若不
        # 复原，兄弟用例（如"从没跑过 → state=none"）会看到残留。逐个快照 +
        # 复原，保证本类对全局状态零副作用。
        # memory._extraction_locks 不在 busref 上（它在 memory 模块里），但同为
        # "以 sid 为键、只增不减"的进程级 dict，一并快照/复原。
        self._dicts = (busref._agents, busref._sigs, busref._ctx,
                       busref._turn_status, busref._running_agents,
                       busref._session_locks, busref._buses,
                       memory._extraction_locks)
        self._snapshots = [dict(d) for d in self._dicts]

    def tearDown(self):
        for d, snap in zip(self._dicts, self._snapshots):
            d.clear()
            d.update(snap)
        super().tearDown()

    def _touch_memory(self, sid):
        """人为把该会话在各内存 dict 里登记一遍（模拟正常使用留下的痕迹）。"""
        busref._agents[sid] = object()
        busref._sigs[sid] = "provider:model:keyhash"
        busref._ctx[sid] = {"prompt_tokens": 1}
        busref._turn_status[sid] = {"outcome": "done", "seen": False}
        busref._running_agents[sid] = object()
        busref._session_locks[sid] = threading.Lock()
        memory._extraction_lock(sid)  # 轮末提取锁，只增不减，同样需要被清

    def test_delete_clears_every_per_session_dict(self):
        sid = "s1"
        self._touch_memory(sid)
        # 只有归档区里的任务允许删除（接口层的硬约束，见 dispatch.do_DELETE）
        db.archive_session(sid, 1)
        status, _ = self.call("DELETE", f"/api/sessions?session_id={sid}")
        self.assertEqual(status, 200)
        for name, d in (("_agents", busref._agents), ("_sigs", busref._sigs),
                        ("_ctx", busref._ctx), ("_turn_status", busref._turn_status),
                        ("_running_agents", busref._running_agents),
                        ("_session_locks", busref._session_locks),
                        ("_buses", busref._buses),
                        ("memory._extraction_locks", memory._extraction_locks)):
            self.assertNotIn(sid, d, f"{name} 未清理——会话删了但内存态留下（慢性泄漏）")

    def test_delete_does_not_touch_other_sessions(self):
        """删除 s1 不能顺手清掉 s2 的内存态（逐键 pop，不是整表 clear）。"""
        self._touch_memory("s1")
        self._touch_memory("s2")
        db.archive_session("s1", 1)
        self.call("DELETE", "/api/sessions?session_id=s1")
        self.assertIn("s2", busref._session_locks)
        self.assertIn("s2", busref._sigs)


if __name__ == "__main__":
    unittest.main()
