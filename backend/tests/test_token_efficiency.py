"""
token 效率优化的单元测试（cd backend && python3 -m unittest tests.test_token_efficiency -v）
============================================================================================

背景：Agent 每轮请求都重发【system + 完整历史 + 工具清单】，一回合 R 轮工具
调用的累计 prompt tokens ≈ O(R²)——这是无状态 API 的成本结构，只能靠两件事
缓解：让重复前缀命中供应商缓存（前缀必须逐字节稳定）、让历史里的"胖材料"
尽早退出。本文件锁定四个优化点不被回退：

1. run_bash 信封去重：命令输出只存 result 一份（曾经 stdout/stderr/result
   三份并存——同一段输出随之后每轮请求重复携带，纯 2 倍浪费）；
2. 记忆索引回合快照：system 是前缀头，回合中途模型写记忆不得改变它
   （否则缓存全灭、整段历史按全价重算）；跨回合必须刷新（新记忆可见）；
3. 工具结果回合开始清理：分级压缩第一档（清旧工具结果）提前到每回合开始
   执行——原本只在估算超窗口 80% 时触发，大窗口下正常对话永远到不了；
   幂等、小结果不动、配对骨架完整；
4. 压缩成本线：COMPACTION_TARGET_TOKENS 让历史瘦身早于窗口 80% 触发
   （窗口=能力上限，成本线=钱袋，两者拆开）。
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import (Agent, CLEAR_TOOL_RESULTS_KEEP_RECENT,
                   CLEARED_TOOL_RESULT_PLACEHOLDER)
from code_tools import run_bash
from memory import INDEX_NAME, memory_dir


def user(text):
    return {"role": "user", "content": text}


def msg(text):
    return {"role": "assistant", "content": text}


def call(cid, name, **args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def tool_result(text, cid):
    return {"role": "tool", "tool_call_id": cid, "content": text}


class RecordingLLM:
    """按剧本逐轮吐消息；每次调用记录完整 messages 快照，可选执行钩子
    （模拟"回合中途磁盘/外部状态发生变化"）。"""

    def __init__(self, script, on_call=None):
        self.script = list(script)
        self.calls = []
        self.on_call = on_call

    def chat_stream(self, messages, tools=None, cancel=None):
        self.calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if self.on_call:
            self.on_call(len(self.calls))
        yield "message", self.script.pop(0)


class AgentBase(unittest.TestCase):

    def make_agent(self, llm=None, **kw):
        ws = tempfile.mkdtemp(prefix="tok_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=llm, verbose=False, workspace=ws, **kw)
        return agent, Path(agent.ctx.workspace)


# ---------------------------------------------------------------------------
# 一、run_bash 信封去重
# ---------------------------------------------------------------------------

class TestRunBashEnvelope(AgentBase):

    def setUp(self):
        _, ws = self.make_agent()
        self.ctx = SimpleNamespace(workspace=str(ws))

    def test_output_stored_exactly_once(self):
        """成功信封：输出只进 result 一份，stdout/stderr 键不再存在——
        每个多余的字段都会随之后每轮请求重复携带。"""
        payload = json.loads(run_bash("echo hello-token-test", ctx=self.ctx))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("hello-token-test", payload["result"])
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)
        self.assertEqual(json.dumps(payload, ensure_ascii=False)
                         .count("hello-token-test"), 1)

    def test_stderr_folded_into_result_once(self):
        """stderr 以 [stderr] 段拼进 result，同样只存一份。"""
        payload = json.loads(run_bash("echo err-msg-42 >&2", ctx=self.ctx))
        self.assertTrue(payload["ok"])
        self.assertIn("[stderr]\nerr-msg-42", payload["result"])
        self.assertEqual(json.dumps(payload, ensure_ascii=False).count("err-msg-42"), 1)

    def test_nonzero_exit_keeps_output_in_result(self):
        """非零退出：失败信封 + 完整输出仍在 result（定位的第一手材料）。"""
        payload = json.loads(run_bash("printf out-x; echo err-y >&2; exit 5", ctx=self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 5)
        self.assertIn("out-x", payload["result"])
        self.assertIn("[stderr]\nerr-y", payload["result"])
        self.assertNotIn("stdout", payload)


# ---------------------------------------------------------------------------
# 二、记忆索引回合快照
# ---------------------------------------------------------------------------

class TestMemorySnapshot(AgentBase):
    """防的事故：system 是每轮请求的前缀头，模型在回合中途写记忆（契约鼓励
    它这么做）若立即反映到 system，本回合后续每一轮的整段前缀缓存全部失效。
    快照保前缀稳定；跨回合刷新保新鲜——两头都不能丢。"""

    def _seed_index(self, ws: Path, line: str):
        mem = memory_dir(ws)
        mem.mkdir(parents=True, exist_ok=True)
        (mem / INDEX_NAME).write_text(line, encoding="utf-8")
        return mem

    def test_snapshot_stable_within_turn(self):
        """同一回合内：中途改写 MEMORY.md，各轮请求的 system 逐字节一致。"""
        agent, ws = self.make_agent()
        mem = self._seed_index(ws, "- [old](old.md) — 旧钩子\n")
        (ws / "a.txt").write_text("内容", encoding="utf-8")

        def hook(n):
            if n == 1:  # 第一轮请求发出后、第二轮之前，模型"写了记忆"
                (mem / INDEX_NAME).write_text("- [new](new.md) — 新钩子\n", encoding="utf-8")

        llm = RecordingLLM([tool_call_message(call("r1", "read_file", path="a.txt")),
                            msg("完成")], on_call=hook)
        agent.llm = llm
        list(agent.run("问"))
        self.assertEqual(len(llm.calls), 2)
        sys0, sys1 = llm.calls[0][0]["content"], llm.calls[1][0]["content"]
        self.assertIn("old.md", sys0)
        self.assertEqual(sys0, sys1, "回合内 system 必须逐字节稳定（前缀缓存前提）")
        self.assertNotIn("new.md", sys1)

    def test_next_turn_sees_fresh_index(self):
        """下一回合开始重新快照：回合间写入的记忆立即可见。"""
        agent, ws = self.make_agent()
        mem = self._seed_index(ws, "- [old](old.md) — 旧钩子\n")
        llm = RecordingLLM([msg("答一"), msg("答二")])
        agent.llm = llm
        list(agent.run("第一问"))
        (mem / INDEX_NAME).write_text("- [fresh](fresh.md) — 新钩子\n", encoding="utf-8")
        list(agent.run("第二问"))
        self.assertIn("old.md", llm.calls[0][0]["content"])
        self.assertIn("fresh.md", llm.calls[1][0]["content"])
        self.assertNotIn("old.md", llm.calls[1][0]["content"])

    def test_direct_system_content_still_reads_live(self):
        """没跑过回合（直接调 _system_content，单测/工具场景）退回现读，
        行为与旧版一致——快照只是回合内的稳定手段，不是新的缓存层。"""
        agent, ws = self.make_agent()
        self.assertNotIn("just-written.md", agent._system_content())
        self._seed_index(ws, "- [just-written](just-written.md) — 即写即见\n")
        self.assertIn("just-written.md", agent._system_content())


# ---------------------------------------------------------------------------
# 三、工具结果回合开始清理（老化退出）
# ---------------------------------------------------------------------------

class TestTurnStartClear(AgentBase):
    """防的事故：工具结果（整文件/命令输出/搜索命中）是历史里最重的重复
    携带，但价值随轮次衰减。清理由每回合开始触发一次（回合内不动——边界
    逐轮前移会打爆前缀缓存），且不删消息骨架（tool 与 assistant.tool_calls
    配对，删了服务商直接 400）。"""

    def _history_with_tool_results(self, n, size, tag="R"):
        hist = [user("原始需求")]
        for i in range(n):
            hist.append(tool_call_message(call(f"c{i}", "read_file", path=f"f{i}.txt")))
            hist.append(tool_result(f"{tag}{i}-" + "x" * size, f"c{i}"))
        return hist

    def test_cleared_at_turn_start_even_with_compaction_off(self):
        """关键缺口回归：context_window=0（压缩关闭）时，回合开始照样清理
        最早的 12-8=4 条大结果——清理不依赖压缩阈值。"""
        llm = RecordingLLM([msg("答")])
        agent, _ = self.make_agent(llm, context_window=0)
        agent.history = self._history_with_tool_results(12, 3000)
        list(agent.run("继续"))
        contents = [m["content"] for m in agent.history if m["role"] == "tool"]
        self.assertEqual(len(contents), 12, "消息骨架一条不删")
        for i in range(4):
            self.assertEqual(contents[i], CLEARED_TOOL_RESULT_PLACEHOLDER,
                             f"第 {i} 条最早结果应被清理")
        for i in range(4, 12):
            self.assertIn(f"R{i}-", contents[i], f"最近 8 条应保持原文（第 {i} 条）")
        # 清理发生在本回合的请求里：模型看到占位符而非旧全文
        request = json.dumps(llm.calls[0], ensure_ascii=False)
        self.assertEqual(request.count(CLEARED_TOOL_RESULT_PLACEHOLDER), 4)
        self.assertNotIn("R0-", request)

    def test_small_results_untouched(self):
        """省不下 CLEAR_TOOL_RESULTS_MIN_SAVING 就不动手（保护原始内容）。"""
        llm = RecordingLLM([msg("答")])
        agent, _ = self.make_agent(llm)
        agent.history = self._history_with_tool_results(12, 10)
        list(agent.run("继续"))
        contents = [m["content"] for m in agent.history if m["role"] == "tool"]
        self.assertTrue(all(c.startswith("R") for c in contents), "小结果全部原样")

    def test_idempotent_across_turns(self):
        """已清理的（占位符）再次清理时被跳过：第二回合不产生新清理。"""
        llm = RecordingLLM([msg("答一"), msg("答二")])
        agent, _ = self.make_agent(llm)
        agent.history = self._history_with_tool_results(12, 3000)
        list(agent.run("继续一"))
        flags_after_first = [m.get("_tool_result_cleared") for m in agent.history
                             if m["role"] == "tool"]
        list(agent.run("继续二"))
        flags_after_second = [m.get("_tool_result_cleared") for m in agent.history
                              if m["role"] == "tool"]
        self.assertEqual(flags_after_first, flags_after_second)
        contents = [m["content"] for m in agent.history if m["role"] == "tool"]
        self.assertEqual(contents.count(CLEARED_TOOL_RESULT_PLACEHOLDER), 4)

    def test_pairing_skeleton_intact_after_clear(self):
        """清理后每个 tool 结果仍与 assistant.tool_calls 按 id 配对、顺序不变
        （服务商配对校验依赖这个）。"""
        agent, _ = self.make_agent(RecordingLLM([msg("答")]))
        agent.history = self._history_with_tool_results(12, 3000)
        list(agent.run("继续"))
        call_ids = [c["id"] for m in agent.history if m.get("tool_calls")
                    for c in m["tool_calls"]]
        result_ids = [m["tool_call_id"] for m in agent.history if m["role"] == "tool"]
        self.assertEqual(result_ids, call_ids)


# ---------------------------------------------------------------------------
# 四、压缩成本线（COMPACTION_TARGET_TOKENS）
# ---------------------------------------------------------------------------

class TestCompactionCostLine(AgentBase):
    """窗口是"模型能吃多少"（能力），成本线是"愿意为单次请求的历史付多少"
    （钱）。成本线让历史瘦身早于窗口 80% 触发；未设置时行为与旧版完全一致。"""

    class SummaryLLM:
        def chat_stream(self, messages, tools=None, cancel=None):
            yield "message", {"role": "assistant", "content": "压缩摘要"}

    def _armed(self, window):
        agent, _ = self.make_agent(self.SummaryLLM(), context_window=window)
        agent.cancel_event = threading.Event()
        agent.history = [user("原始需求")] + [msg("长内容" * 300) for _ in range(12)]
        return agent

    def test_target_triggers_compaction_below_window(self):
        """成本线 300 tokens ≪ 窗口：即使窗口巨大也触发压缩。"""
        agent = self._armed(window=10 ** 9)
        with mock.patch.dict(os.environ, {"COMPACTION_TARGET_TOKENS": "300"}):
            self.assertIsNotNone(agent._maybe_compact())
        self.assertTrue(any(m["role"] == "compact" for m in agent.history))

    def test_no_target_keeps_window_only_behavior(self):
        """未设置（现网默认）：大窗口下不压缩，行为与旧版一致。"""
        agent = self._armed(window=10 ** 9)
        with mock.patch.dict(os.environ, {"COMPACTION_TARGET_TOKENS": ""}):
            self.assertIsNone(agent._maybe_compact())
        self.assertFalse(any(m["role"] == "compact" for m in agent.history))

    def test_target_never_exceeds_window_line(self):
        """成本线取与窗口线的较小者：target 巨大时仍由窗口线（80%）决定。"""
        agent = self._armed(window=1000)  # 窗口线 = 800 tokens，估算必然超过
        with mock.patch.dict(os.environ, {"COMPACTION_TARGET_TOKENS": "999999999"}):
            self.assertIsNotNone(agent._maybe_compact())

    def test_garbage_target_treated_as_off(self):
        """环境变量是垃圾值（"abc"）按未设置处理，绝不因配置炸掉回合。"""
        agent = self._armed(window=10 ** 9)
        with mock.patch.dict(os.environ, {"COMPACTION_TARGET_TOKENS": "abc"}):
            self.assertIsNone(agent._maybe_compact())


if __name__ == "__main__":
    unittest.main(verbosity=2)
