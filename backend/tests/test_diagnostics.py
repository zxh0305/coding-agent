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
        os.environ.pop("DIAGNOSTICS", None)
        self.addCleanup(lambda: os.environ.pop("DIAGNOSTICS", None))

    def _read(self, rec):
        path = diagnostics.turn_path(rec.sid, rec.started_at, rec.user_mid)
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

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

    def test_sid_path_traversal_is_contained(self):
        rec = diagnostics.TurnRecorder("../../etc", "mid5")
        rec.round_start(1)
        rec.finish(outcome="done")
        written = list(self.root.rglob("*.jsonl"))
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
