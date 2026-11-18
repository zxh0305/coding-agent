"""backend/diagnostics.py 的单元测试。

重点验证五件事：
 1. 正常回合能写出完整的事件序列与终态汇总；
 2. 未结算的工具在收尾时会被补记（不出现「开始无结束」）；
 3. 关闭开关后不产生任何文件；
 4. 写盘失败绝不影响回合（不抛异常）；
 5. sid 里的路径穿越字符不会逃出诊断目录。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import diagnostics


class TurnRecorderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(diagnostics, "_DIAG_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # ROLLUP_PATH 在模块加载时就算好了，只 patch _DIAG_DIR 会让测试把
        # 合成分回合写进**生产**的 data/diagnostics/rollup.jsonl —— 真实发生
        # 过一次（99 行全是 sess1/../../etc 之类的测试数据，把线上成功率
        # 统计污染成 81%）。这里必须一起隔离。
        rollup = self.root / "rollup.jsonl"
        rp = mock.patch.object(diagnostics, "ROLLUP_PATH", rollup)
        rp.start()
        self.addCleanup(rp.stop)
        os.environ.pop("DIAGNOSTICS", None)
        self.addCleanup(lambda: os.environ.pop("DIAGNOSTICS", None))

    def _read(self, rec):
        path = diagnostics.turn_path(rec.sid, rec.started_at, rec.user_mid)
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    def test_tests_never_touch_production_rollup(self):
        """护栏用例：跑一遍完整记录流程，确认生产 rollup 文件没被写。

        这条防的是"测试污染真实指标"。曾经发生过：99 行合成数据进了生产
        rollup，把成功率统计成 81%（实际没有任何真实回合）。
        护栏比事后清理可靠——它会在每次 CI 里跑，而不是等人发现数字不对。
        """
        # 注意：不能读 diagnostics.ROLLUP_PATH —— setUp 已经把它 patch 到临时目录了，
        # 那样测的就是"临时文件没被动"，等于什么都没测。这里必须硬编码真实路径。
        prod = diagnostics._PROJECT_DIR / "data" / "diagnostics" / "rollup.jsonl"
        before = prod.stat().st_size if prod.exists() else None

        rec = diagnostics.TurnRecorder("guard-session", "guardmid")
        rec.round_start(1)
        rec.finish(outcome="done")

        after = prod.stat().st_size if prod.exists() else None
        self.assertEqual(before, after, f"测试写进了生产 rollup：{prod}")

    def test_normal_turn_writes_full_sequence(self):
        rec = diagnostics.TurnRecorder("sess1", "abcdef123456")
        rec.turn_start(model_input_chars=42)
        rec.round_start(1)
        rec.llm_first_token(1)
        rec.tool_start("read_file")
        rec.tool_end("read_file", ok=True)
        rec.round_end(1)
        rec.finish(outcome="done")

        events = self._read(rec)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["turn_start", "round_start", "llm_first",
                                 "tool_start", "tool_end", "round_end", "turn_end"])
        end = events[-1]["extra"]
        self.assertEqual(end["outcome"], "done")
        self.assertEqual(end["rounds"], 1)
        self.assertGreaterEqual(end["rounds"], 1)
        # 工具耗时被计入汇总
        self.assertGreaterEqual(events[4]["extra"]["duration_ms"], 0)
        self.assertGreaterEqual(end["tool_total_s"], 0)

    def test_unsettled_tool_is_backfilled(self):
        rec = diagnostics.TurnRecorder("sess2", "mid00000")
        rec.round_start(1)
        rec.tool_start("run_bash")   # 模拟回合异常跳出，没有 tool_end
        rec.finish(outcome="error", error_class="fatal")

        events = self._read(rec)
        backfilled = [e for e in events if e["kind"] == "tool_end"]
        self.assertEqual(len(backfilled), 1)
        self.assertTrue(backfilled[0]["extra"]["unsettled"])
        self.assertFalse(backfilled[0]["extra"]["ok"])

    def test_disabled_writes_nothing(self):
        os.environ["DIAGNOSTICS"] = "0"
        rec = diagnostics.TurnRecorder("sess3", "mid3")
        rec.round_start(1)
        rec.finish(outcome="done")
        self.assertFalse(any(self.root.rglob("*.jsonl")))

    def test_flush_failure_never_raises(self):
        rec = diagnostics.TurnRecorder("sess4", "mid4")
        rec.round_start(1)
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            rec.finish(outcome="done")   # 不应抛出

    def test_rollup_written_and_readable(self):
        """每回合写一行汇总供指标聚合（含失败回合，这是 message_usage 缺的）。"""
        rollup = self.root / "rollup.jsonl"
        with mock.patch.object(diagnostics, "ROLLUP_PATH", rollup):
            for i, outcome in enumerate(("done", "error")):
                rec = diagnostics.TurnRecorder(f"s{i}", f"m{i}")
                rec.round_start(1)
                rec.finish(outcome=outcome, retryable=(outcome == "error"))
            rows = diagnostics.read_rollup(None)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["outcome"] for r in rows], ["done", "error"])
        self.assertTrue(rows[1]["retryable"])
        self.assertEqual(rows[0]["sid"], "s0")

    def test_rollup_bad_lines_are_skipped(self):
        """历史文件可能被截断出半行：坏行跳过，不能整份读挂。"""
        rollup = self.root / "rollup.jsonl"
        rollup.write_text('{"outcome":"done"}\n{"broken\n{"outcome":"error"}\n', encoding="utf-8")
        with mock.patch.object(diagnostics, "ROLLUP_PATH", rollup):
            rows = diagnostics.read_rollup(None)
        self.assertEqual(len(rows), 2)

    def test_sid_path_traversal_is_contained(self):
        rec = diagnostics.TurnRecorder("../../etc", "mid5")
        rec.round_start(1)
        rec.finish(outcome="done")
        # 只数回合明细；rollup.jsonl 现在也在临时目录里（会被上面的 patch 隔离到这里）
        written = [p for p in self.root.rglob("*.jsonl") if p.name != "rollup.jsonl"]
        self.assertEqual(len(written), 1)
        self.assertTrue(str(written[0]).startswith(str(self.root)))

    def test_prune_keeps_recent_files(self):
        for i in range(diagnostics.KEEP_PER_SESSION + 5):
            rec = diagnostics.TurnRecorder("sess6", f"mid{i:04d}", started_at=1_700_000_000 + i)
            rec.round_start(1)
            rec.finish(outcome="done")
        remaining = list((self.root / "sess6").glob("*.jsonl"))
        self.assertEqual(len(remaining), diagnostics.KEEP_PER_SESSION)


if __name__ == "__main__":
    unittest.main()
