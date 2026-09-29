"""
用量归账的单元测试（cd backend && python3 -m unittest tests.test_usage_accounting -v）
======================================================================================

隐藏 LLM 消耗（子代理侦察、上下文压缩）此前不落账，用量页永远比账单少一截。
覆盖：
1. 存储面：迁移 25 后 kind 列存在；record_usage 独立行落库（合成 mid、
   三列展开、stats_json 无损）；非法 kind 回退 'turn'；
2. 聚合口径：usage_summary / usage_session_rows 的 turns 只数主回合行
   （COALESCE 兜住老数据 NULL），token 三列含全部 kind（账单口径）；
3. agent 面：子代理收尾经 usage_sink 记账（正常结论 / 被停止的半程）；
   压缩成功记账、失败不记；sink 未注入 = 丢弃（CLI 口径）；sink 异常不打断回合。
"""

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import db
from agent import Agent
from tools import ToolContext


def make_agent(ws, llm=None, **kw):
    if llm is None:
        class N:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "message", {"role": "assistant", "content": "ok"}
        llm = N()
    return Agent(llm=llm, verbose=False, workspace=str(ws), **kw)


class StorageBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage_acct_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.ws = Path(self.tmp) / "ws"
        self.ws.mkdir()

    def tearDown(self):
        db.DB_PATH = self._orig

    def rows(self, sql, *params):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()


class TestRecordUsage(StorageBase):
    def test_independent_row_with_synthetic_mid(self):
        db.record_usage("s1", "subagent", {"task": "侦察", "usage": {
            "prompt_tokens": 100, "completion_tokens": 10,
            "prompt_cache_hit_tokens": 40}}, "prov", "glm-4")
        row = self.rows("SELECT * FROM message_usage WHERE session_id='s1'")[0]
        self.assertTrue(row["mid"].startswith("subagent:"))
        self.assertEqual(row["kind"], "subagent")
        self.assertEqual(row["prompt_tokens"], 100)
        self.assertEqual(row["completion_tokens"], 10)
        self.assertEqual(row["cached_tokens"], 40)
        self.assertEqual(row["provider_id"], "prov")
        self.assertEqual(row["model"], "glm-4")
        self.assertIn("侦察", row["stats_json"])
        self.assertIsNotNone(row["created"])

    def test_unknown_kind_falls_back_to_turn(self):
        db.record_usage("s1", "bogus", {"usage": {"prompt_tokens": 1}})
        row = self.rows("SELECT kind FROM message_usage")[0]
        self.assertEqual(row["kind"], "turn")

    def test_zero_usage_still_records_schema_shape(self):
        """无 usage（空账）也允许落——调用方负责跳过；这里只锁列形状不炸。"""
        db.record_usage("s1", "compact", {"est_tokens": 5})
        row = self.rows("SELECT kind, prompt_tokens FROM message_usage")[0]
        self.assertEqual(row["kind"], "compact")
        self.assertIsNone(row["prompt_tokens"])


class TestAggregation(StorageBase):
    def _seed(self):
        # 主回合行（走 save_messages 的 _stats 通道）
        msgs = [{"role": "user", "content": "q", "_mid": "mu"},
                {"role": "assistant", "content": "a", "_mid": "ma",
                 "_stats": {"usage": {"prompt_tokens": 500, "completion_tokens": 50,
                                      "prompt_cache_hit_tokens": 100},
                            "provider_id": "prov", "model": "glm-4"}}]
        db.save_messages("s1", msgs, {})
        # 子代理 + 压缩：独立归账行
        db.record_usage("s1", "subagent", {"usage": {"prompt_tokens": 300,
                                                     "completion_tokens": 30}}, "prov", "glm-4")
        db.record_usage("s1", "compact", {"usage": {"prompt_tokens": 200,
                                                    "completion_tokens": 20}}, "prov", "glm-4")

    def test_turns_counts_main_turns_only_tokens_sum_all(self):
        self._seed()
        summary = db.usage_summary()
        self.assertEqual(len(summary), 1)
        row = summary[0]
        self.assertEqual(row["turns"], 1)                 # 子代理/压缩不算回合数
        self.assertEqual(row["prompt_tokens"], 1000)      # 账单口径：全部 kind 求和
        self.assertEqual(row["completion_tokens"], 100)
        sessions = db.usage_session_rows()
        self.assertEqual(sessions[0]["turns"], 1)
        self.assertEqual(sessions[0]["prompt_tokens"], 1000)

    def test_legacy_null_kind_counts_as_turn(self):
        """迁移前落库的行 kind 为 NULL（ALTER DEFAULT 不回填存量）：
        COALESCE 口径必须把它当主回合数、token 照常求和。"""
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT INTO message_usage(session_id, mid, prompt_tokens, "
                         "completion_tokens, cached_tokens, stats_json, provider_id, model, "
                         "created) VALUES('s1','legacy',10,1,0,'{}','prov','glm-4',1)")
        summary = db.usage_summary()
        self.assertEqual(summary[0]["turns"], 1)
        self.assertEqual(summary[0]["prompt_tokens"], 10)


class UsageLLM:
    """剧本 LLM：每条响应前先产一条 usage（模拟流式请求的用量 chunk，累计
    口径——每轮发同一条，最后一个即全程）。is_child 按系统提示识别。"""

    def __init__(self, turns, usage, cancel_on_child_first=False):
        self.turns = list(turns)
        self.usage = usage
        self.cancel_on_child_first = cancel_on_child_first
        self.requests = []
        self.child_calls = 0

    def chat_stream(self, messages, tools=None, cancel=None):
        is_child = (messages[0].get("content") or "").startswith("你是只读侦察子代理")
        self.requests.append({"messages": messages, "child": is_child})
        if is_child:
            self.child_calls += 1
            if self.cancel_on_child_first and self.child_calls == 1 and cancel is not None:
                cancel.set()
        if not self.turns:
            raise AssertionError("剧本耗尽后仍在调用 LLM")
        turn = self.turns.pop(0)
        message = {"role": "assistant", "content": turn.get("content")}
        calls = [{"id": "c1", "type": "function",
                  "function": {"name": c["name"],
                               "arguments": json.dumps(c.get("arguments") or {})}}
                 for c in (turn.get("tool_calls") or [])]
        if calls:
            message["tool_calls"] = calls
        return iter([("usage", dict(self.usage)), ("message", message)])


class TestAgentSideAccounting(StorageBase):
    def test_subagent_success_records_usage(self):
        (self.ws / "a.txt").write_text("hello", encoding="utf-8")
        llm = UsageLLM([
            {"tool_calls": [{"name": "spawn_subagent",
                             "arguments": {"tasks": ["读 a.txt"]}}]},
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            {"content": "结论：hello"},
            {"content": "收到"},
        ], usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                  "prompt_cache_hit_tokens": 50, "prompt_cache_miss_tokens": 50})
        agent = make_agent(self.ws, llm=llm)
        records = []
        agent.usage_sink = lambda kind, stats: records.append((kind, stats))
        list(agent.run("派子代理"))
        self.assertEqual([k for k, _ in records], ["subagent"])
        stats = records[0][1]
        self.assertTrue(stats["ok"])
        # done.usage 是子代理全程的累计口径：2 轮请求 × 每轮 prompt 100 = 200
        self.assertEqual(stats["usage"]["prompt_tokens"], 200)
        self.assertEqual(stats["usage"]["prompt_cache_hit_tokens"], 100)  # 缓存命中列的数据源

    def test_subagent_stopped_halfway_still_records(self):
        """被停止的半程侦察同样烧了 token——归账不能漏。"""
        llm = UsageLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"tasks": ["侦察"]}}]},
            {"content": "半截话"},
        ], usage={"prompt_tokens": 70, "completion_tokens": 7, "total_tokens": 77},
           cancel_on_child_first=True)
        agent = make_agent(self.ws, llm=llm)
        records = []
        agent.usage_sink = lambda kind, stats: records.append((kind, stats))
        list(agent.run("派子代理"))
        self.assertEqual([k for k, _ in records], ["subagent"])
        stats = records[0][1]
        self.assertFalse(stats["ok"])
        self.assertEqual(stats["usage"]["prompt_tokens"], 70)  # 来自 usage 事件兜底

    def test_compact_success_records_and_failure_does_not(self):
        agent = make_agent(self.ws)
        records = []
        agent.usage_sink = lambda kind, stats: records.append((kind, stats))

        class Summarizer:
            def __init__(self, boom=False):
                self.boom = boom

            def chat_stream(self, messages, tools=None, cancel=None):
                if self.boom:
                    raise RuntimeError("网络中断")
                yield ("usage", {"prompt_tokens": 900, "completion_tokens": 90})
                yield ("message", {"role": "assistant", "content": "摘要内容"})

        agent.history = [{"role": "user", "content": "锚点"}] + [
            {"role": "user" if i % 2 else "assistant", "content": f"m{i}"}
            for i in range(10)]
        agent.cancel_event = threading.Event()  # run() 之外直调须自备停止开关
        agent.llm = Summarizer()
        payload = agent._maybe_compact(force=True)
        self.assertIsNotNone(payload)  # 压缩成功
        self.assertEqual([k for k, _ in records], ["compact"])
        self.assertEqual(records[0][1]["usage"]["prompt_tokens"], 900)

        # 失败路径：不计一次账，也不计入熔断之外的状态
        records.clear()
        agent.llm = Summarizer(boom=True)
        agent.history = [{"role": "user", "content": "锚点"}] + [
            {"role": "user" if i % 2 else "assistant", "content": f"m{i}"}
            for i in range(10)]
        self.assertIsNone(agent._maybe_compact(force=True))
        self.assertEqual(records, [])

    def test_no_sink_is_silent_and_sink_exception_is_swallowed(self):
        agent = make_agent(self.ws)  # CLI 口径：无 sink
        agent._record_usage("subagent", {"usage": {"prompt_tokens": 1}})  # 不抛即过
        def boom(kind, stats):
            raise RuntimeError("账本炸了")
        agent.usage_sink = boom
        agent._record_usage("subagent", {"usage": {"prompt_tokens": 1}})  # 只记日志
        agent._record_usage("subagent", {"usage": {}})  # 空 usage 不触发 sink


if __name__ == "__main__":
    unittest.main(verbosity=2)
