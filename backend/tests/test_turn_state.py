"""回合状态机（session_turns，迁移 27）回归测试。

验收标准是"重启后能说清上一轮怎么了"：

1. 生命周期：开始 → running，轮号随轮推进，收尾 → done/error 且 ended 有值；
2. **启动改判**：进程退出时残留的 running 一律变成 interrupted——这是整个
   功能的核心，少了它前端依旧谎报"运行中"；
3. 会话列表读出的 state 不再一律 none：中断/未读在重启后依然可见，看过
   之后回到 none；内存有记录时以内存为准（同一台账，内存更即时）；
4. 清理：删会话、回退编辑都要连状态行一起清，不留孤儿。

（cd backend && python3 -m unittest tests.test_turn_state -v）
"""

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import db


class TurnStateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="turn_state_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.addCleanup(setattr, db, "DB_PATH", self._orig)

    def test_lifecycle(self):
        db.turn_started("s1", "u1")
        row = db.get_turn("s1", "u1")
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["ended"])

        db.turn_round("s1", "u1", 3)
        self.assertEqual(db.get_turn("s1", "u1")["rounds"], 3)

        db.turn_finished("s1", "u1", "done")
        row = db.get_turn("s1", "u1")
        self.assertEqual(row["state"], "done")
        self.assertIsNotNone(row["ended"])
        self.assertGreaterEqual(row["ended"], row["started"])

    def test_error_carries_reason(self):
        db.turn_started("s1", "u1")
        db.turn_finished("s1", "u1", "error", "余额不足")
        self.assertEqual(db.get_turn("s1", "u1")["error"], "余额不足")

    def test_rejects_interrupted_as_terminal_state(self):
        """interrupted 只能由启动改判写入，业务代码不能直接写——否则"进程死了"
        这个唯一证据会被伪装成普通收尾。"""
        db.turn_started("s1", "u1")
        with self.assertRaises(ValueError):
            db.turn_finished("s1", "u1", "interrupted")

    def test_restart_marks_running_interrupted(self):
        db.turn_started("s1", "u1")
        db.turn_started("s1", "u2")
        db.turn_finished("s1", "u2", "done")

        n = db.mark_running_turns_interrupted()
        self.assertEqual(n, 1, "只有仍在 running 的那条被改判")
        self.assertEqual(db.get_turn("s1", "u1")["state"], "interrupted")
        self.assertEqual(db.get_turn("s1", "u2")["state"], "done", "已收尾的不受影响")

    def test_restart_is_idempotent(self):
        db.turn_started("s1", "u1")
        self.assertEqual(db.mark_running_turns_interrupted(), 1)
        self.assertEqual(db.mark_running_turns_interrupted(), 0, "第二次没有可改判的")

    def test_restarted_turn_starts_fresh(self):
        """同一 user_mid 重新开跑（不应发生，但接口要幂等）：running 覆盖旧终态。"""
        db.turn_started("s1", "u1")
        db.turn_finished("s1", "u1", "error", "x")
        db.turn_started("s1", "u1")
        row = db.get_turn("s1", "u1")
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["ended"])
        self.assertIsNone(row["error"], "重开的回合不能带着上一轮的错因")

    def test_latest_turns_picks_newest(self):
        db.turn_started("s1", "u1", started=100.0)
        db.turn_finished("s1", "u1", "done")
        db.turn_started("s1", "u2", started=200.0)
        latest = db.latest_turns(["s1"])
        self.assertEqual(latest["s1"]["user_mid"], "u2")

    def test_latest_turns_batch(self):
        db.create_session("s2", 1)
        db.turn_started("s1", "u1")
        db.turn_started("s2", "v1")
        got = db.latest_turns(["s1", "s2", "missing"])
        self.assertEqual(set(got), {"s1", "s2"})

    def test_get_turns_batch(self):
        db.turn_started("s1", "u1")
        db.turn_started("s1", "u2")
        got = db.get_turns("s1", ["u1", "u2", "nope"])
        self.assertEqual(set(got), {"u1", "u2"})
        self.assertEqual(got["u1"]["state"], "running")

    def test_seen_persists(self):
        db.turn_started("s1", "u1")
        db.turn_finished("s1", "u1", "done")
        db.turn_seen("s1", "u1")
        self.assertEqual(db.get_turn("s1", "u1")["seen"], 1)

    def test_delete_session_cleans_turns(self):
        db.turn_started("s1", "u1")
        db.delete_session("s1")
        self.assertIsNone(db.get_turn("s1", "u1"))

    def test_truncate_cleans_turns(self):
        """回退到某轮起点：该轮及之后的回合状态一并作废（重发会拿新 mid）。"""
        db.save_messages("s1", [
            {"role": "user", "content": "问题1", "_mid": "u1"},
            {"role": "assistant", "content": "回答1", "_mid": "a1"},
            {"role": "user", "content": "问题2", "_mid": "u2"},
            {"role": "assistant", "content": "回答2", "_mid": "a2"},
        ], {})
        for mid in ("u1", "u2"):
            db.turn_started("s1", mid)
            db.turn_finished("s1", mid, "done")

        db.truncate_from("s1", "u2")
        self.assertIsNotNone(db.get_turn("s1", "u1"), "保留段的回合状态不动")
        self.assertIsNone(db.get_turn("s1", "u2"), "被回退的回合状态必须清掉")


class SessionStateTest(unittest.TestCase):
    """app._session_state：内存优先、库里兜底。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sess_state_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.addCleanup(setattr, db, "DB_PATH", self._orig)
        import app
        from state import _buses, _turn_status
        self.app = app
        self._buses, self._turn_status = _buses, _turn_status
        self.addCleanup(_buses.clear)
        self.addCleanup(_turn_status.clear)

    def test_interrupted_visible_after_restart(self):
        db.turn_started("s1", "u1")
        db.mark_running_turns_interrupted()
        self.assertEqual(self.app._session_state("s1"), "interrupted")

    def test_seen_interrupted_falls_back_to_none(self):
        db.turn_started("s1", "u1")
        db.mark_running_turns_interrupted()
        db.turn_seen("s1", "u1")
        self.assertEqual(self.app._session_state("s1"), "none")

    def test_unseen_done_from_db(self):
        db.turn_started("s1", "u1")
        db.turn_finished("s1", "u1", "done")
        self.assertEqual(self.app._session_state("s1"), "done")
        db.turn_seen("s1", "u1")
        self.assertEqual(self.app._session_state("s1"), "none")

    def test_running_in_db_but_no_live_bus_reports_interrupted(self):
        """库里还挂着 running 但内存没有任何活的 bus：绝不谎报"运行中"。"""
        db.turn_started("s1", "u1")
        self.assertEqual(self.app._session_state("s1"), "interrupted")

    def test_memory_wins(self):
        """内存里有本轮结果（比库里新）：以内存为准，不去查库。"""
        db.turn_started("s1", "u1")
        db.mark_running_turns_interrupted()          # 库里是 interrupted
        self._turn_status["s1"] = {"outcome": "done", "seen": False}
        self.assertEqual(self.app._session_state("s1"), "done")

    def test_idle_session_is_none(self):
        self.assertEqual(self.app._session_state("s1"), "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
