"""
压缩可靠性与重试观测的单元测试（cd backend && python3 -m unittest tests.test_reliability -v）
==============================================================================================

对标 ZCode 的压缩工程保险（policy.ts / compact-post-reminders.ts / microcompact.ts）
与 apiRetry 观测，覆盖六项：

1. CJK 感知 token 估算：中文按 ~0.67 token/字、英文按 ~0.25 token/字符，
   修复旧 0.4 统一系数对中文系统性低估（压缩与清理因此迟到）；
2. 压缩熔断：摘要连续失败 3 次后停止自动尝试——LLM 没余额/挂掉时不再
   每个回合收尾白付一次注定失败的调用；成功清零；
3. 摘要提示词升级：必须包含「用户消息逐字列出」「安全红线」「下一步」小节；
4. compact 后文件重注入：最近读过的文件重放为合成消息（不落库），预算
   封顶、超限降级为一行引用、保留段里已有的跳过；
5. 工具结果清理白名单：失败结果（error/权限拒绝）豁免——清掉它模型会重踩；
6. API 重试观测：post_json_with_retry 每次决定重试时回调 on_retry，
   回调炸掉不干扰重试主流程。
"""

import json
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import llm_client
from agent import (Agent, MAX_COMPACT_FAILURES, REINJECT_MAX_FILES,
                   REINJECT_TOTAL_CHARS, SUMMARIZE_PROMPT, estimate_tokens)
from llm_client import ApiHTTPError, post_json_with_retry


def user(text):
    return {"role": "user", "content": text}


def msg(text):
    return {"role": "assistant", "content": text}


class AgentBase(unittest.TestCase):

    def make_agent(self, llm=None, **kw):
        ws = tempfile.mkdtemp(prefix="rel_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        return Agent(llm=llm, verbose=False, workspace=ws, **kw)

    def _armed(self, llm, window=300):
        agent = self.make_agent(llm, context_window=window)
        agent.cancel_event = threading.Event()
        agent.history = [user("原始需求")] + [msg("长内容" * 300) for _ in range(12)]
        return agent


# ---------------------------------------------------------------------------
# 一、CJK 感知估算
# ---------------------------------------------------------------------------

class TestEstimateTokens(unittest.TestCase):

    def test_cjk_and_latin_densities(self):
        """中文 ~0.67 token/字、英文 ~0.25 token/字符：旧 0.4 系数对中文
        低估一半、对英文高估——两个方向都要在合理区间内。"""
        zh = "你好世界" * 100                       # 400 个 CJK 字符
        en = "word " * 100                          # 500 ASCII 字符
        zh_est = estimate_tokens(zh)
        en_est = estimate_tokens(en)
        self.assertTrue(250 <= zh_est <= 300, zh_est)   # 0.63~0.75 / 字
        self.assertTrue(150 <= en_est <= 180, en_est)   # 公式对英文 ~0.33/字符（偏高=安全侧）
        self.assertEqual(estimate_tokens(""), 0)

    def test_context_stats_uncalibrated_uses_cjk(self):
        """无校准系数时（首轮/压缩后）：中文历史的估算显著高于旧 0.4 口径
        （4 万中文字符：旧口径 16000，新口径 ~26000）——压缩不再迟到。"""
        agent = AgentBase.make_agent(self)
        agent.history = [user("长" * 40000)]
        stats = agent.context_stats()
        self.assertGreater(stats["user"], 40000 * 0.5)
        self.assertLess(stats["user"], 40000 * 0.8)
        self.assertGreater(stats["system"] + stats["tools"], 0)

    def test_context_stats_calibrated_overrides(self):
        """有真实 prompt_tokens 时仍走校准分摊（最准的口径不被替换）。"""
        agent = AgentBase.make_agent(self)
        agent.history = [user("中文内容"), msg("reply")]
        stats = agent.context_stats(prompt_tokens=1000)
        self.assertEqual(sum(stats.values()), 1000)
        self.assertIsNotNone(agent._token_ratio)


# ---------------------------------------------------------------------------
# 二、压缩熔断
# ---------------------------------------------------------------------------

class BoomLLM:
    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None, cancel=None):
        self.calls += 1
        raise RuntimeError("网络错误")
        yield  # pragma: no cover


class TestCompactBreaker(AgentBase):

    def test_breaks_after_consecutive_failures(self):
        """连续 3 次失败后熔断：第 4 次不再调用 LLM（白付的调用省掉了）。"""
        llm = BoomLLM()
        agent = self._armed(llm)
        results = [agent._maybe_compact() for _ in range(4)]
        self.assertEqual(results[:3], [None, None, None])
        self.assertEqual(llm.calls, 3, "前 3 次各尝试一次")
        self.assertEqual(agent._compact_fail_streak, MAX_COMPACT_FAILURES)
        self.assertIsNone(results[3])
        self.assertEqual(llm.calls, 3, "熔断后第 4 次不再调用")

    def test_success_resets_streak(self):
        """成功一次即清零：失败 2 次 → 成功 → 熔断计数归零。"""

        class FlakyLLM:
            def __init__(self):
                self.calls = 0

            def chat_stream(self, messages, tools=None, cancel=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("抖动")
                yield "message", {"role": "assistant", "content": "摘要"}

        llm = FlakyLLM()
        agent = self._armed(llm)
        self.assertIsNone(agent._maybe_compact())   # 失败
        self.assertEqual(agent._compact_fail_streak, 1)
        agent.history = [user("原始需求")] + [msg("长内容" * 300) for _ in range(12)]
        self.assertIsNotNone(agent._maybe_compact())  # 成功
        self.assertEqual(agent._compact_fail_streak, 0)

    def test_cancelled_summary_not_counted(self):
        """被停止掐断/空摘要不算失败（不是服务商的错，不该推进熔断）。"""

        class EmptyLLM:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "message", {"role": "assistant", "content": ""}

        agent = self._armed(EmptyLLM())
        self.assertIsNone(agent._maybe_compact())
        self.assertEqual(agent._compact_fail_streak, 0)

    def test_prompt_contains_verbatim_and_safety_sections(self):
        """摘要提示词必须带「用户原话逐字」「安全红线」「下一步」三节——
        压缩后的失忆感主要来自丢用户原话。"""
        self.assertIn("逐字", SUMMARIZE_PROMPT)
        self.assertIn("下一步", SUMMARIZE_PROMPT)
        self.assertIn("禁止", SUMMARIZE_PROMPT)
        self.assertIn("任务目标", SUMMARIZE_PROMPT)


# ---------------------------------------------------------------------------
# 三、compact 后文件重注入
# ---------------------------------------------------------------------------

class TestPostCompactReminder(AgentBase):

    def test_recent_files_reinjected_newest_first(self):
        reads = [("a.py", "content-a"), ("b.py", "content-b")]
        out = Agent._build_post_compact_reminder("", reads)
        self.assertIn("content-b", out)
        self.assertIn("content-a", out)
        self.assertLess(out.index("content-b"), out.index("content-a"), "最新在前")
        self.assertIn("重新 read_file", out)  # 提醒可能过期

    def test_budget_caps_files_and_total(self):
        reads = [(f"f{i}.py", "x" * 5000) for i in range(10)]
        out = Agent._build_post_compact_reminder("", reads)
        self.assertLessEqual(len(out), REINJECT_TOTAL_CHARS + 2000)
        injected = sum(1 for ln in out.splitlines()
                      if ln.startswith("### ") and "未注入" not in ln)
        self.assertEqual(injected, REINJECT_MAX_FILES, "最多注入 5 个文件，其余降级为引用")

    def test_file_in_preserved_tail_skipped(self):
        reads = [("kept.py", "already visible"), ("gone.py", "was summarized away")]
        out = Agent._build_post_compact_reminder("前面保留段 already visible 原文", reads)
        self.assertIn("was summarized away", out)
        self.assertNotIn("### kept.py", out, "保留段里已有的文件不重注入")

    def test_empty_reads_empty_output(self):
        self.assertEqual(Agent._build_post_compact_reminder("", []), "")
        self.assertEqual(Agent._build_post_compact_reminder("", None), "")

    def test_compact_appends_synthetic_reminder_not_persisted(self):
        """端到端：压缩成功后历史追加 _synthetic 重注入消息；发给模型时
        content 保留、内部标记被剥（不落库由 save_messages 的 _synthetic
        跳过保证，test_storage 已有覆盖）。"""
        class SummaryLLM:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "message", {"role": "assistant", "content": "摘要"}

        agent = self._armed(SummaryLLM())
        agent.recent_reads = [("notes.py", "IMPORTANT-CONSTANT = 1")]
        self.assertIsNotNone(agent._maybe_compact())
        reminders = [m for m in agent.history if m.get("_synthetic")]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["role"], "user")
        self.assertIn("IMPORTANT-CONSTANT", reminders[0]["content"])
        cleaned = agent._clean_outgoing(reminders[0])
        self.assertIn("IMPORTANT-CONSTANT", cleaned["content"])
        self.assertNotIn("_synthetic", cleaned)


# ---------------------------------------------------------------------------
# 四、清理白名单：失败结果豁免
# ---------------------------------------------------------------------------

class TestClearWhitelist(AgentBase):

    def test_error_results_never_cleared(self):
        """较早的失败结果（工具报错/权限拒绝）不被清理：清掉它，模型再遇
        同类场景会原样重踩。成功的大结果照常清理。"""
        agent = self.make_agent()
        agent.history = [user("需求")]
        for i in range(12):
            agent.history.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "run_bash", "arguments": "{}"}}]})
            if i in (0, 5):  # 两条较早的失败结果（必落在清理目标区）
                agent.history.append({"role": "tool", "tool_call_id": f"c{i}",
                                      "content": json.dumps({"ok": False, "error": f"boom {i}",
                                                             "hint": "先读 stderr"})})
            else:
                agent.history.append({"role": "tool", "tool_call_id": f"c{i}",
                                      "content": json.dumps({"ok": True,
                                                             "result": "x" * 3000})})
        cleared = agent._clear_old_tool_results(keep_recent=8)
        self.assertGreater(cleared, 0)
        contents = {m["tool_call_id"]: m["content"] for m in agent.history
                    if m["role"] == "tool"}
        self.assertIn("boom 0", contents["c0"])
        self.assertIn("boom 5", contents["c5"])
        self.assertNotIn("x" * 3000, contents["c1"], "成功结果被清理")


# ---------------------------------------------------------------------------
# 五、API 重试观测
# ---------------------------------------------------------------------------

class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


class TestRetryObservation(unittest.TestCase):

    def test_on_retry_called_with_progress(self):
        """每次决定重试都回调：attempt 递增、wait 为退避秒数；成功后不再回调。"""
        events = []
        responses = [ApiHTTPError(503, "overloaded"),
                     ApiHTTPError(429, "rate limited"),
                     FakeResponse()]

        def fake_post(*a, **k):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(llm_client, "_http_post_json", side_effect=fake_post), \
             mock.patch.object(llm_client, "_sleep_cancellable", return_value=True):
            post_json_with_retry("http://x", {}, {}, 5, attempts=3,
                                 on_retry=lambda info: events.append(info))
        self.assertEqual([e["attempt"] for e in events], [1, 2])
        self.assertEqual(events[0]["max_attempts"], 3)
        self.assertGreater(events[0]["wait"], 0)

    def test_on_retry_failure_does_not_break_retry(self):
        """回调炸掉只记日志：重试主流程照常。"""
        responses = [ApiHTTPError(503, "x"), FakeResponse()]

        def boom(info):
            raise RuntimeError("回调坏了")

        def fake_post(*a, **k):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(llm_client, "_http_post_json", side_effect=fake_post), \
             mock.patch.object(llm_client, "_sleep_cancellable", return_value=True) as slp:
            post_json_with_retry("http://x", {}, {}, 5, attempts=2, on_retry=boom)
            self.assertEqual(slp.call_count, 1, "回调炸了也要继续等待重试")

    def test_no_retry_no_callback(self):
        """不可重试错误（400）直接抛：不回调。"""
        with mock.patch.object(llm_client, "_http_post_json",
                               side_effect=ApiHTTPError(400, "bad")):
            with self.assertRaises(ApiHTTPError):
                post_json_with_retry("http://x", {}, {}, 5,
                                     on_retry=lambda info: self.fail("不该回调"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
