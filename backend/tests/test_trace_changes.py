"""轨迹行里的附加字段（changes）存取回归测试。

回归背景：回合结束时有两个东西要落进 session_traces——轨迹数组（键 =
answer_mid）与回合改动（键 = user_mid）。第一版实现里该表一行只能放一个
JSON 值：轨迹用整体覆盖写，改动也用整体覆盖写，谁后写谁赢。实测答案是
先写轨迹、后写 changes，轨迹就被挤掉了（回放时"执行过程"折叠条消失）。

现在行的形状是可选的 {"trace": [...], "changes": {...}}，两边各写各的键；
get_traces 只返回数组、get_trace_extras 只返回附加字段——旧格式（纯数组）
必须继续被正确读取。

（cd backend && python3 -m unittest tests.test_trace_changes -v）
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import db


class TraceChangesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trace_changes_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.addCleanup(setattr, db, "DB_PATH", self._orig)

    def test_legacy_array_row_still_reads(self):
        """迁移前的纯数组行：get_traces 照常返回，extras 为空。"""
        db.set_trace("s1", "m1", json.dumps([{"type": "round", "round": 1}]))
        self.assertEqual(db.get_traces("s1", ["m1"])["m1"][0]["type"], "round")
        self.assertEqual(db.get_trace_extras("s1", ["m1"]), {})
        self.assertEqual(db.get_trace("s1", "m1")[0]["round"], 1)

    def test_changes_does_not_evict_trace(self):
        """同一 mid 上先写轨迹、再写 changes：两者都在。"""
        db.set_trace("s1", "m1", json.dumps([{"type": "tool_call", "name": "x"}]))
        db.merge_trace_field("s1", "m1", "changes",
                             {"stat": " a.py | 2 +-", "diff": "+x", "truncated": False})

        traces = db.get_traces("s1", ["m1"])
        self.assertEqual(len(traces["m1"]), 1, "轨迹数组必须还在")
        self.assertEqual(traces["m1"][0]["name"], "x")
        self.assertEqual(db.get_trace_extras("s1", ["m1"])["m1"]["changes"]["stat"],
                         " a.py | 2 +-")

    def test_production_order_keeps_both(self):
        """生产顺序：收尾时先把轨迹写进 answer_mid，再把 changes 写进
        user_mid（同一回合，两个不同的 mid）。两条都要读得到——这是历史
        回放"执行过程 + 改动"都能显示的前提。"""
        answer_mid, user_mid = "a1", "u1"
        db.set_trace("s1", answer_mid, json.dumps([{"type": "round", "round": 1}]))
        db.merge_trace_field("s1", user_mid, "changes",
                             {"stat": " a.py | 2 +-", "diff": "+x"})

        self.assertEqual(db.get_traces("s1", [answer_mid])[answer_mid][0]["round"], 1)
        self.assertEqual(db.get_trace_extras("s1", [user_mid])[user_mid]["changes"]["diff"],
                         "+x")
        # 两个键互不串台：轨迹查询拿不到 changes，附加字段查询拿不到轨迹
        self.assertNotIn(answer_mid, db.get_trace_extras("s1", [answer_mid]))
        self.assertNotIn(user_mid, db.get_traces("s1", [user_mid]))

    def test_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            db.merge_trace_field("s1", "m3", "trace", [])

    def test_merge_is_idempotent(self):
        for _ in range(3):
            db.merge_trace_field("s1", "m4", "changes", {"stat": "s", "diff": "d"})
        self.assertEqual(db.get_trace_extras("s1", ["m4"])["m4"]["changes"]["diff"], "d")

    def test_corrupt_row_ignored(self):
        db.set_trace("s1", "m5", "{not json")
        self.assertEqual(db.get_traces("s1", ["m5"]), {})
        self.assertIsNone(db.get_trace("s1", "m5"))
        # 损坏行上再写附加字段不应抛：按"没有轨迹"处理
        db.merge_trace_field("s1", "m5", "changes", {"stat": "s", "diff": "d"})
        self.assertEqual(db.get_trace_extras("s1", ["m5"])["m5"]["changes"]["diff"], "d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
