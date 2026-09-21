"""
上下文压缩的单元测试（纯标准库 unittest，运行：cd backend && python3 -m unittest tests.test_compact -v）
================================================================================

重点测【模型视图构建】（_visible_history / _messages_for_model）：这是"两套视图一个
真相"的核心——压缩后发给模型的消息必须以摘要替代中段、逐字保留首条用户消息、
且绝不混进 role=compact 标记或 _stats 内部字段（后者发给服务商会 400）。

端到端（_maybe_compact）用假 LLM 驱动，不碰网络。真实踩坑的两类暗坑各有专项：
  * 切点悬空：保留段以 role=tool 开头、或被压缩段以带 tool_calls 的 assistant 结尾，
    两种都会让下一次真实请求被服务商直接拒收；
  * 视图下标 → 原始历史下标 的映射：无边界时视图就是 history 本身、锚点是
    first_user+1 而不是 last_boundary+1，差一位就会把边界插错位置、吞掉活消息。
"""

import shutil
import tempfile
import threading
import unittest

from agent import Agent, COMPACT_MIN_SEGMENT, COMPACT_SUMMARY_NOTE, COMPACT_KEEP_TAIL


# ---------- 消息构造小工具 ----------

def user(text, **extra):
    return {"role": "user", "content": text, **extra}


def asst(text, with_stats=False):
    m = {"role": "assistant", "content": text}
    if with_stats:  # 真实历史里助手消息带 _stats（发给模型前必须被剥掉）
        m["_stats"] = {"elapsed_s": 1.2, "usage": {"total_tokens": 9}}
    return m


def tool_call_asst(call_id="c1", name="run_bash"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": "{\"command\": \"ls\"}"}}]}


def tool_result(text, call_id="c1"):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def boundary(summary):
    """压缩边界标记（_maybe_compact 落进 history 的形状）。"""
    return {"role": "compact", "content": summary,
            "is_compact_boundary": True, "_stats": {"compacted": True}}


class FakeLLM:
    """总结调用的假客户端：记录请求、吐出固定摘要。delta 片段也吐一条，
    验证压缩路径确实丢弃增量（不产生回答流）。"""

    def __init__(self, summary="摘要：已修复 demo.py 并验证通过"):
        self.summary = summary
        self.calls = []

    def chat_stream(self, messages, tools=None, cancel=None):
        self.calls.append(messages)
        yield "delta", "（不应出现在任何地方的增量）"
        yield "message", {"role": "assistant", "content": self.summary}


class ExplodingLLM:
    """模拟网络错误：压缩必须吞掉异常、跳过本轮，绝不让会话崩掉。"""

    def chat_stream(self, messages, tools=None, cancel=None):
        raise RuntimeError("模拟网络错误")
        yield  # 使之成为生成器函数（raise 在首个 next 才执行）


class CompactTestBase(unittest.TestCase):
    def make_agent(self, history=None, llm=None, **kw) -> Agent:
        # 独立临时工作区：Agent.__init__ 会 prepare_workspace，别让它碰项目目录
        ws = tempfile.mkdtemp(prefix="compact_test_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=llm, verbose=False, workspace=ws, **kw)
        if history is not None:
            agent.history = history
        return agent


# ---------------------------------------------------------------------------
# 模型视图构建：给定消息列表 + 边界 → 期望的模型视图
# ---------------------------------------------------------------------------

class TestVisibleHistory(CompactTestBase):

    def test_no_compact_passes_everything(self):
        """从未压缩：视图应原样全量返回（现网最长走的路径，先守住基线）。"""
        agent = self.make_agent(history=[
            user("帮我修 demo.py 的 bug"), asst("好的，我先看看"),
            user("修好了吗"), asst("修好并验证了"),
        ])
        view = agent._messages_for_model()
        self.assertEqual([m["role"] for m in view], ["user", "assistant", "user", "assistant"])
        self.assertEqual(view[0]["content"], "帮我修 demo.py 的 bug")

    def test_after_compact_middle_replaced_by_summary(self):
        """压缩后：首条用户消息逐字保留 + 摘要以 user 身份顶替中段 + 边界后原样。"""
        agent = self.make_agent(history=[
            user("原始需求：重构登录模块"),
            asst("我先列一下计划"), user("计划可以，开始吧"), asst("第一步完成"),
            boundary("已完成登录模块重构的第一步"),
            user("继续第二步"), asst("第二步也完成了"),
        ])
        view = agent._messages_for_model()
        # 结构：[原始需求, 摘要(user), 保留段...]，中段 4 条被替代
        self.assertEqual([m["role"] for m in view],
                         ["user", "user", "user", "assistant"])
        self.assertEqual(view[0]["content"], "原始需求：重构登录模块")  # 逐字，不走摘要
        self.assertIn(COMPACT_SUMMARY_NOTE, view[1]["content"])
        self.assertIn("第一步", view[1]["content"])
        self.assertEqual(view[2]["content"], "继续第二步")
        self.assertEqual(view[3]["content"], "第二步也完成了")

    def test_append_after_compact_stays_visible(self):
        """压缩后又追加新消息：新消息必须落在边界之后的保留段里，对模型可见。"""
        agent = self.make_agent(history=[
            user("原始需求"), asst("开工"), boundary("摘要：开工了"),
            user("问题一"), asst("回答一"),
            user("压缩之后新提的问题"), asst("压缩之后的新回答", with_stats=True),
        ])
        view = agent._messages_for_model()
        self.assertEqual(view[-2]["content"], "压缩之后新提的问题")
        self.assertEqual(view[-1]["content"], "压缩之后的新回答")
        self.assertNotIn("_stats", view[-1])  # 内部字段仍被正常剥离（压缩不能破坏原逻辑）

    def test_double_compact_only_last_boundary_wins(self):
        """连续两次压缩：只有最后一条边界生效——旧摘要已被新摘要吸收（再总结的
        输入包含旧摘要），若视图里同时保留两个摘要，等于让模型看两遍互相矛盾的历史。"""
        agent = self.make_agent(history=[
            user("原始需求"),
            asst("旧消息1"), asst("旧消息2"),
            boundary("第一次摘要"),
            asst("中段消息1"), asst("中段消息2"),
            boundary("第二次摘要（含第一次的内容）"),
            user("最新问题"), asst("最新回答"),
        ])
        view = agent._messages_for_model()
        self.assertEqual([m["role"] for m in view], ["user", "user", "user", "assistant"])
        self.assertEqual(view[0]["content"], "原始需求")
        self.assertNotIn("第一次摘要", "".join(str(m.get("content")) for m in view))
        self.assertIn("第二次摘要", view[1]["content"])
        self.assertEqual(view[-2]["content"], "最新问题")

    def test_marker_and_stats_never_reach_model(self):
        """边界标记本身与 _stats 绝不能进模型视图（前者是我们的私造 role、
        后者部分服务商对未知字段直接 400）。"""
        agent = self.make_agent(history=[
            user("需求"), asst("答", with_stats=True), boundary("摘要"),
            tool_call_asst(), tool_result("ok"), asst("收尾", with_stats=True),
        ])
        view = agent._messages_for_model()
        self.assertFalse(any(m["role"] == "compact" for m in view))
        self.assertFalse(any(k.startswith("_") for m in view for k in m))

    def test_context_stats_counts_view_not_raw_history(self):
        """容量估算必须按模型视图口径：压缩后被压缩段已不再发给模型，若仍按
        原始历史估算，会永远"超阈值"，陷入无意义的反复压缩。"""
        agent = self.make_agent(history=[
            user("需求"),
            asst("超长中段" * 2000),  # ~1 万字，被压缩掉
            boundary("短摘要"),
            user("新问题"), asst("新回答"),
        ])
        stats = agent.context_stats()  # 无校准系数 → 粗略 0.4 token/字符
        self.assertLess(stats["user"] + stats["assistant"], 500)  # 只算摘要+尾部
        self.assertGreater(stats["system"] + stats["tools"], 0)   # 构成仍在


class TestCompactSplit(CompactTestBase):
    """切点选择：必须落在完整对话回合之间（纯函数）。"""

    def test_plain_history_cut_keeps_tail(self):
        view = [user("需求")] + [asst(f"a{i}") for i in range(11)]
        cut = Agent._compact_split(view, head=1)
        self.assertEqual(cut, len(view) - COMPACT_KEEP_TAIL)          # 无悬空直接切
        self.assertEqual(len(view) - cut, COMPACT_KEEP_TAIL)          # 保留最近 6 条

    def test_cut_walks_back_when_tail_starts_with_tool(self):
        # len-6 处恰是 tool 结果 → 切点左移到"调用它之前"，不能让保留段开头悬空
        view = [user("需求"), asst("a"), tool_call_asst("c1"),
                tool_result("r1"), asst("a2"), tool_call_asst("c2"),
                tool_result("r2"), asst("a3"), user("u1"), asst("a4"),
                user("u2"), asst("a5")]
        cut = Agent._compact_split(view, head=1)
        self.assertNotEqual(view[cut]["role"], "tool")
        # 切点之前那条若是带 tool_calls 的 assistant 同样不行（悬空调用留在压缩段结尾）
        prev = view[cut - 1]
        self.assertFalse(prev.get("role") == "assistant" and prev.get("tool_calls"))
        self.assertEqual(len(view) - cut, 7)  # 从 6 回退到 7：view[6] 是 c2 的 tool 结果

    def test_cut_rejects_dangling_tool_call_at_segment_end(self):
        # 期望切点 view[cut-1] 是 tool_call_asst → 再左移一位，把它一起压进摘要
        view = [user("需求"), asst("a1"), user("u"), asst("a2"), user("u2"),
                tool_call_asst("c9"), tool_result("r9"), asst("a3"),
                user("u3"), asst("a4"), user("u4"), asst("a5")]
        cut = Agent._compact_split(view, head=1)
        self.assertIsNotNone(cut)
        prev = view[cut - 1]
        self.assertFalse(prev.get("role") == "assistant" and prev.get("tool_calls"))

    def test_too_short_segment_returns_none(self):
        view = [user("需求"), asst("a1"), asst("a2"), asst("a3"), asst("a4"),
                asst("a5"), asst("a6")]  # 可压缩段凑不满 COMPACT_MIN_SEGMENT
        self.assertIsNone(Agent._compact_split(view, head=1))
        self.assertLess(COMPACT_MIN_SEGMENT, COMPACT_KEEP_TAIL + 4)  # 常量合理性自检


# ---------------------------------------------------------------------------
# 端到端：_maybe_compact（假 LLM，不碰网络）
# ---------------------------------------------------------------------------

class TestMaybeCompact(CompactTestBase):

    HISTORY = [
        user("原始需求：给 demo.py 加测试"),
        asst("先读文件"), asst("写用例"), tool_call_asst("c1"), tool_result(" PASS "),
        asst("跑通了"), user("再加一个边界用例"), asst("好的"),
        # ↓ 靠后的回合（压缩时原样保留；切点回退后实际保留可能多于 6 条）
        tool_call_asst("c2"), tool_result(" PASS 2"), asst("两用例都通过"),
        user("顺便修下 README"), tool_call_asst("c3"), tool_result(" patched "), asst("完成"),
    ]

    def _armed(self, llm, window=300):
        agent = self.make_agent(history=list(self.HISTORY), llm=llm, context_window=window)
        agent.cancel_event = threading.Event()
        return agent

    def test_triggers_inserts_boundary_and_invalidates_ratio(self):
        llm = FakeLLM(summary="任务摘要：demo.py 已加两个测试并修了 README")
        agent = self._armed(llm)
        agent._token_ratio = 0.5  # 预置校准系数：压缩后必须作废
        before = list(agent.history)

        result = agent._maybe_compact()
        self.assertIsNotNone(result)
        self.assertEqual(result["summary"], "任务摘要：demo.py 已加两个测试并修了 README")

        # history 只多一条边界标记，前缀与尾部对象原封不动（真相不动，只插标记）
        idx = [i for i, m in enumerate(agent.history) if m["role"] == "compact"]
        self.assertEqual(len(idx), 1)
        i = idx[0]
        self.assertEqual(agent.history[:i], before[:i])
        self.assertEqual(agent.history[i + 1:], before[i:])
        marker = agent.history[i]
        self.assertTrue(marker["is_compact_boundary"])
        self.assertTrue(marker["_stats"]["compacted"])
        self.assertLess(i, len(before) - COMPACT_KEEP_TAIL)  # 边界必须在保留段之前

        # 校准系数作废（摘要的 token 密度与原始历史不同，旧系数会带偏估算）
        self.assertIsNone(agent._token_ratio)

        # 视图合法：首条用户消息逐字、摘要顶替中段、尾部无悬空 tool 开头
        view = agent._messages_for_model()
        self.assertEqual(view[0]["content"], "原始需求：给 demo.py 加测试")
        self.assertNotEqual(view[2]["role"], "tool")
        self.assertIn("任务摘要", view[1]["content"])

        # 总结调用：发给"当前会话同一个 LLM"，prompt 是中文压缩指令 + 文本纪要
        self.assertEqual(len(llm.calls), 1)
        prompt = llm.calls[0][0]["content"]
        self.assertIn("任务目标", prompt)          # 中文总结提示词的必备要素
        self.assertIn("待压缩的对话记录", prompt)   # 纪要正文已拼进
        self.assertIn("run_bash", prompt)           # 工具调用进了纪要

    def test_below_threshold_no_llm_call(self):
        llm = FakeLLM()
        agent = self._armed(llm, window=10 ** 9)  # 窗口巨大 → 估算不可能超 80%
        self.assertIsNone(agent._maybe_compact())
        self.assertEqual(llm.calls, [])
        self.assertEqual(len(agent.history), len(self.HISTORY))

    def test_llm_failure_skips_and_retries_later(self):
        """网络错误：本轮跳过、历史不动、不抛异常——下一轮回答结束后自然重试。"""
        agent = self._armed(ExplodingLLM())
        n = len(agent.history)
        self.assertIsNone(agent._maybe_compact())  # 不抛异常即通过
        self.assertEqual(len(agent.history), n)
        self.assertFalse(any(m["role"] == "compact" for m in agent.history))

    def test_disabled_when_no_window(self):
        """context_window=0（未配置）→ 完全不压缩，行为与改造前一致。"""
        agent = self._armed(FakeLLM(), window=0)
        self.assertIsNone(agent._maybe_compact())

    def test_run_yields_compacted_after_done(self):
        """run() 的钩子：done 之后（未被停止时）跟随 compacted 事件，且事件
        不携带 answer 流——前端靠它渲染分隔卡片。"""
        events = []

        def fake_run(u, um):  # 顶替 _run：直接产出一次正常结束的回答
            yield "done", {"answer": "回答完毕"}

        agent = self._armed(FakeLLM(), window=300)
        agent._run = fake_run
        for kind, payload in agent.run("随便问点长的"):
            events.append((kind, payload))
        self.assertEqual([k for k, _ in events], ["done", "compacted"])
        self.assertIn("summary", events[-1][1])

    def test_run_skips_compact_when_stopped(self):
        """用户主动停止时不再压缩：他刚表达"想停下"，不能又塞一次隐性 LLM 等待。"""

        def fake_run(u, um):
            yield "done", {"answer": "半截", "stopped": True}

        llm = FakeLLM()
        agent = self._armed(llm, window=300)
        agent._run = fake_run
        kinds = [kind for kind, _ in agent.run("问")]
        self.assertEqual(kinds, ["done"])
        self.assertEqual(llm.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
