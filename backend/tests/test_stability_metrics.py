"""backend/stability_metrics.py 的单测：口径正确性，而不是数字好看。

最关键的三条口径：
 1. 用户主动停止不算失败（否则成功率会被自己的操作拉低，误判系统不稳）；
 2. 空样本不崩、不返回 NaN，且「—」由前端渲染；
 3. 工具耗时占比能区分「慢在模型」与「慢在工具」。
"""

import unittest

from stability_metrics import aggregate


def row(outcome="done", dur=10.0, rounds=2, tool=1.0, ttft=1.5, stopped=False, retryable=False):
    return {"outcome": outcome, "duration_s": dur, "rounds": rounds,
            "tool_total_s": tool, "ttft_s": ttft, "stopped": stopped,
            "retryable": retryable}


class AggregateTest(unittest.TestCase):
    def test_empty_is_safe(self):
        m = aggregate([])
        self.assertEqual(m["turns"], 0)
        self.assertEqual(m["success_rate"], 0.0)
        self.assertIsNone(m["duration_s"]["p50"])
        self.assertIsNone(m["ttft_s"]["p95"])
        self.assertIsNone(m["tool_time_share"])
        self.assertEqual(m["by_outcome"], {"done": 0, "error": 0, "stopped": 0})

    def test_success_error_split(self):
        rows = [row("done"), row("done"), row("error", retryable=True), row("error")]
        m = aggregate(rows)
        self.assertEqual(m["turns"], 4)
        self.assertEqual(m["by_outcome"], {"done": 2, "error": 2, "stopped": 0})
        self.assertEqual(m["success_rate"], 0.5)
        self.assertEqual(m["error_rate"], 0.5)
        self.assertEqual(m["retryable_errors"], 1)

    def test_stopped_is_not_failure(self):
        """用户点停止必须单独计数，不能算失败——这是口径最容易搞错的地方。"""
        rows = [row("done"), row("done"), row("stopped", stopped=True)]
        m = aggregate(rows)
        self.assertEqual(m["by_outcome"]["stopped"], 1)
        self.assertEqual(m["by_outcome"]["error"], 0)
        self.assertAlmostEqual(m["success_rate"], 2 / 3, places=4)
        self.assertEqual(m["error_rate"], 0.0)

    def test_outcome_done_but_stopped_flag_wins(self):
        """stopped 标记优先于 outcome 文案，避免停止路径写成 done 时被算成成功。"""
        m = aggregate([row("done", stopped=True)])
        self.assertEqual(m["by_outcome"]["stopped"], 1)
        self.assertEqual(m["by_outcome"]["done"], 0)

    def test_percentiles_nearest_rank(self):
        durs = [row(dur=d) for d in (1, 2, 3, 4, 100)]
        m = aggregate(durs)
        self.assertEqual(m["duration_s"]["p50"], 3.0)
        self.assertEqual(m["duration_s"]["p95"], 100.0)
        self.assertEqual(m["duration_s"]["max"], 100.0)

    def test_missing_fields_are_skipped_not_zeroed(self):
        """缺字段的行（老数据/失败回合没吐字）不参与分位，不能当 0 秒拉低指标。"""
        rows = [row(ttft=None), row(ttft=10.0), row(dur=None, ttft=None)]
        m = aggregate(rows)
        self.assertEqual(m["ttft_s"]["p50"], 10.0)
        self.assertEqual(m["turns"], 3)          # 回合数仍然算 3
        self.assertEqual(m["duration_s"]["p50"], 10.0)

    def test_tool_time_share(self):
        m = aggregate([row(dur=100, tool=70), row(dur=100, tool=10)])
        self.assertAlmostEqual(m["tool_time_share"], 0.4, places=3)

    def test_rounds_avg(self):
        m = aggregate([row(rounds=2), row(rounds=4)])
        self.assertEqual(m["rounds_per_turn"]["avg"], 3.0)


if __name__ == "__main__":
    unittest.main()
