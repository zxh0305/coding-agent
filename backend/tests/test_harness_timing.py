"""
Harness 式可观测埋点的单元测试
==================================================================
运行：cd backend && python3 -m unittest tests.test_harness_timing -v

覆盖 2026-10 新增的响应可观测链路：
1. llm_client 的 _timing 元数据（TTFT / gen_s）——用一个假的流式客户端直接验；
2. agent 的轮级统计：usage 事件带 round / round_cache_hit_rate；
3. agent 的工具计时：tool_result 事件与 trace 带 dur_s；
4. agent 的回合汇总：done 事件与 _stats 带 timing（llm_total_s / tool_total_s /
   ttft_avg_s / tps / cache_hit_rate）；
5. trace 条目统一带 ts 时间戳。

这几项是"看得见慢"的地基——没有它们，排查耗时只能靠翻日志重建时间线。
"""

import tempfile
import shutil
import time
import unittest
from pathlib import Path

import db
from agent import Agent


class FakeLLM:
    """按脚本回放：每次 chat_stream 依次吐 reasoning/delta/message，message 带
    _timing（模拟 llm_client 的真实产出）。脚本元素：("text", s) 正文增量、
    ("tool", call_dict) 工具调用。
    """

    def __init__(self, script):
        self.script = script          # list[list[step]]：每轮一组 step
        self.calls = 0

    def chat_stream(self, messages, tools=None, cancel=None):
        steps = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        # 模拟一点生成耗时与首 token 延迟
        time.sleep(0.01)
        yield "delta", "".join(s[1] for s in steps if s[0] == "text")
        # usage chunk（真实客户端每轮结束前会发）——agent 的轮级统计依赖它
        yield "usage", {"prompt_tokens": 1000, "completion_tokens": 8,
                        "total_tokens": 1008,
                        "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}
        msg = {"role": "assistant", "content": "",
               "tool_calls": [s[1] for s in steps if s[0] == "tool"]}
        msg["_timing"] = {"ttft_s": 0.5, "gen_s": 2.0}
        yield "message", msg


class HitOnlyLLM(FakeLLM):
    """只返回 cache_read 不返回 cache_creation 的网关（maiya 生产实测行为）。
    曾导致 agent.py 里 hit + miss = int + NoneType，崩掉整个回合
    （2026-10-12 线上事故：TypeError in _consume_stream）。"""

    def chat_stream(self, messages, tools=None, cancel=None):
        yield "usage", {"prompt_tokens": 1000, "completion_tokens": 8,
                        "prompt_cache_hit_tokens": 1024}  # 无 miss 键
        yield "message", {"role": "assistant", "content": "done"}


def collect(gen):
    """把 agent.run() 的 (kind, payload) 生成器收成列表。"""
    return list(gen)


class HarnessTimingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.ws = Path(self.tmp) / "ws"
        self.ws.mkdir()

    def tearDown(self):
        db.DB_PATH = self._orig


class TestTimingChain(HarnessTimingBase):
    def test_agent_stats_and_events_carry_timing(self):
        # 两轮：第一轮调一次 read_file，第二轮收尾出正文（走"做完"路径）
        llm = FakeLLM([
            [("tool", {"id": "c1", "type": "function",
                       "function": {"name": "list_dir", "arguments": '{"path":"."}'}})],
            [("text", "完成")],
        ])
        ag = Agent(llm=llm, verbose=False, workspace=str(self.ws),
                   session_id="s1")
        events = collect(ag.run("读一下目录"))

        kinds = [k for k, _ in events]
        self.assertIn("usage", kinds)
        self.assertIn("done", kinds)

        # usage 事件带轮级字段
        usage = [p for k, p in events if k == "usage"][0]
        self.assertIn("round", usage)
        self.assertIsNotNone(usage.get("round"))

        # done 事件带 timing 汇总
        done = [p for k, p in events if k == "done"][-1]
        self.assertIn("timing", done)
        t = done["timing"]
        self.assertGreater(t["llm_total_s"], 0)      # 两轮 gen_s=2.0 各计
        self.assertAlmostEqual(t["llm_total_s"], 4.0, places=1)
        self.assertGreaterEqual(t["tool_total_s"], 0)  # list_dir 很快，>=0
        self.assertEqual(t["ttft_avg_s"], 0.5)         # 两轮各 0.5，均值 0.5
        self.assertIsNotNone(t["tps"])                 # 有输出即应算出速度

        # 落库的 _stats 也应带 timing（最后一条 assistant）
        stats = None
        for m in reversed(ag.history):
            if m.get("role") == "assistant" and m.get("_stats"):
                stats = m["_stats"]
                break
        self.assertIsNotNone(stats)
        self.assertIn("llm_total_s", stats)
        self.assertIn("tool_total_s", stats)
        self.assertIn("tps", stats)

    def test_tool_result_carries_dur_and_trace_has_ts(self):
        llm = FakeLLM([
            [("tool", {"id": "c1", "type": "function",
                       "function": {"name": "list_dir", "arguments": '{"path":"."}'}})],
            [("text", "好")],
        ])
        ag = Agent(llm=llm, verbose=False, workspace=str(self.ws),
                   session_id="s1")
        events = collect(ag.run("读目录"))
        tr = [p for k, p in events if k == "tool_result"]
        self.assertTrue(tr, "应有 tool_result 事件")
        self.assertIn("dur_s", tr[0], "tool_result 应带 dur_s")
        self.assertIsInstance(tr[0]["dur_s"], (int, float))

        # 所有 trace 条目都应带 ts
        missing = [e for e in ag.trace if "ts" not in e]
        self.assertEqual(missing, [], f"有 trace 条目缺 ts: {missing[:3]}")


class TestCacheHitOnly(HarnessTimingBase):
    def test_cache_hit_only_no_crash(self):
        """回归：网关只返回 cache_hit 不返回 miss（2026-10-12 线上 TypeError，
        崩掉两个回合）。整个回合必须能跑完、done 带正常 timing。"""
        llm = HitOnlyLLM([[("text", "好")]])
        ag = Agent(llm=llm, verbose=False, workspace=str(self.ws),
                   session_id="s1")
        events = collect(ag.run("测一下"))
        kinds = [k for k, _ in events]
        self.assertIn("done", kinds)  # 没崩、正常收尾
        done = [p for k, p in events if k == "done"][-1]
        self.assertIn("timing", done)


if __name__ == "__main__":
    unittest.main()
