"""Agent 装配（session_boot.get_session）的回归测试
================================================================================
运行：cd backend && python3 -m unittest tests.test_session_boot -v

两件事都以"改了不报错、只在特定条件下才出问题"为特征，所以需要显式锁住：

1. 配置指纹必须含 context_window。
   指纹决定"何时重建 Agent"。窗口是自动压缩触发线的基准，改了窗口却不重建，
   实例就仍按旧窗口判断——【把窗口改小】时最危险：该压缩却不压，直接撞真实
   上限。（改大只是压缩偏早，浪费上下文，不致命；改小会炸。）

2. 历史恢复不得在全局锁内做。
   _lock 保护的是跨会话的 _agents/_sigs 字典。历史恢复要读库
   （count_messages/restore_window/fingerprints），大会话上是实打实的 I/O；
   若在锁内，一个大会话的恢复会卡住【所有】会话的提问/删除/停止。
"""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  导入即 install_service_hooks()，注入 session_boot 的依赖
import db  # noqa: E402
from services import session_boot  # noqa: E402
from services.model_resolve import _resolve_client, _window_for  # noqa: E402


class BootBase(unittest.TestCase):
    """独占临时库 + 一套最小供应商（复用 SessionModelBase 的重定向套路）。"""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="session_boot_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_user("tester", "pw")
        self.uid = db.get_user_by_name("tester")["id"]
        db.delete_provider("default")
        db.set_setting("active_model", {})
        db.upsert_provider("p1", "A", "http://a", "k1", True, "openai", 100000)
        db.upsert_model("p1", "a-big", 200000, True)
        db.set_setting("active_model", {"provider_id": "p1", "model": "a-big"})
        # 每个用例从干净的缓存起步（这些是跨用例共享的模块级字典）
        session_boot._agents.clear()
        session_boot._sigs.clear()
        self.addCleanup(session_boot._agents.clear)
        self.addCleanup(session_boot._sigs.clear)

    def tearDown(self):
        db.DB_PATH = self._orig


class TestConfigFingerprint(BootBase):

    def test_window_is_part_of_fingerprint(self):
        """同一模型改窗口 → 指纹必须变，才会重建 Agent 让新阈值生效。"""
        _, _, _, sig_before, _ = _resolve_client(None)
        db.upsert_model("p1", "a-big", 4000, True)     # 把窗口改小
        _, _, _, sig_after, _ = _resolve_client(None)
        self.assertNotEqual(sig_before, sig_after,
                            "改了 context_window 指纹却没变——压缩仍按旧窗口判断，"
                            "窗口改小时会不压缩而直接撞上限")

    def test_shrinking_window_rebuilds_agent(self):
        """端到端：改窗口后 get_session 应交回【新的】Agent 实例。"""
        db.create_session("s1", self.uid)
        _, a1 = session_boot.get_session("s1", self.uid)
        self.assertEqual(a1.context_window, 200000)
        db.upsert_model("p1", "a-big", 4000, True)
        _, a2 = session_boot.get_session("s1", self.uid)
        self.assertIsNot(a1, a2, "窗口变了但实例没重建")
        self.assertEqual(a2.context_window, 4000, "新实例的压缩阈值没跟着更新")

    def test_same_config_reuses_instance(self):
        """没改配置时不得重建（重建会白读一次库、丢掉实例上的运行时状态）。"""
        db.create_session("s1", self.uid)
        _, a1 = session_boot.get_session("s1", self.uid)
        _, a2 = session_boot.get_session("s1", self.uid)
        self.assertIs(a1, a2)

    def test_window_for_matches_active_window(self):
        """_window_for 与 _active_window 必须是同一套优先级（改一处不会漏另一处）。

        两处取同一个 (provider, model) 比：分别把激活模型设成这两个模型，
        再比较两个函数的返回值，避免用"会话当前激活的那个"去比"另一个模型"。
        """
        from services.model_resolve import _active_window
        db.create_session("s2", self.uid)

        # 情形一：模型自填窗口
        db.set_setting("active_model", {"provider_id": "p1", "model": "a-big"})
        self.assertEqual(_window_for(db.get_provider("p1"), "a-big"),
                         _active_window("s2"))

        # 情形二：模型未自填 → 回落供应商默认列
        db.upsert_provider("p1", "A", "http://a", "k1", True, "openai", 7777)
        db.upsert_model("p1", "c-nofill", 0, True)
        db.set_setting("active_model", {"provider_id": "p1", "model": "c-nofill"})
        self.assertEqual(_window_for(db.get_provider("p1"), "c-nofill"),
                         _active_window("s2"))
        self.assertEqual(_active_window("s2"), 7777)   # 确实是回落值


class TestLockScope(BootBase):

    def test_history_restore_does_not_hold_global_lock(self):
        """历史恢复期间全局锁必须可被别的会话拿到。

        做法：把一个"别的会话"的取用放在恢复过程中并发执行。若恢复在锁内，
        它必须等恢复完（这里让恢复 sleep 一段可控时间），我们据此判定——
        用 count_messages 打桩注入延迟，模拟大会话的慢恢复。
        """
        import services.session_boot as sb
        db.create_session("slow", self.uid)
        db.create_session("other", self.uid)

        orig_count = sb.db.count_messages
        entered = threading.Event()

        def slow_count(sid):
            if sid == "slow":
                entered.set()
                time.sleep(0.6)   # 模拟大会话的慢恢复
            return orig_count(sid)

        sb.db.count_messages = slow_count
        self.addCleanup(lambda: setattr(sb.db, "count_messages", orig_count))

        t = threading.Thread(target=lambda: sb.get_session("slow", self.uid))
        t.start()
        entered.wait(3)
        # slow 的恢复正在进行；此刻取另一个会话应当【不必等它结束】
        t0 = time.time()
        sb.get_session("other", self.uid)
        elapsed = time.time() - t0
        t.join(5)
        self.assertLess(elapsed, 0.5,
                        f"另一个会话的取用被慢恢复阻塞了 {elapsed:.2f}s——"
                        f"说明历史恢复仍在全局锁内，大会话会卡住所有会话")

    def test_concurrent_same_session_yields_one_instance(self):
        """并发取同一会话：最终缓存里只有一个实例，各调用方拿到同一个。

        锁外建实例会短暂产生两个候选，登记时以先登记者为准——不能出现
        "两个线程各拿一个实例去跑回合"（那会让 agent.saved 账本与历史错位）。
        """
        db.create_session("c1", self.uid)
        got = []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            try:
                _, agent = session_boot.get_session("c1", self.uid)
                got.append(id(agent))
            except Exception as e:  # pragma: no cover
                got.append(f"ERR:{type(e).__name__}:{e}")

        ths = [threading.Thread(target=worker) for _ in range(6)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        errs = [g for g in got if isinstance(g, str)]
        self.assertEqual(errs, [], f"并发取用报错：{errs}")
        self.assertEqual(len(set(got)), 1, f"并发后出现多个实例：{got}")
        self.assertEqual(len(session_boot._agents), 1)


if __name__ == "__main__":
    unittest.main()
