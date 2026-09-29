"""
子代理 MVP 专项测试（cd backend && python3 -m unittest tests.test_subagent -v）
================================================================================

覆盖 spawn_subagent 读侧扇出的六个关键面：
  1. schema/注册/只读标记接线（17 号工具面、串行）；
  2. 完整回放：父派出 → 子只读侦察 → 结论回填，父子上下文互不渗漏；
  3. 子代理写操作与递归派生在白名单闸被拒（schema 看不见 + 执行层把关）；
  4. 取消传播：父「停止」共享开关，子代理即时收场、不烧多余 LLM 调用；
  5. 轮数耗尽走收尾轮，结论仍以子代理自己的总结收场；
  6. 报告超长软闸截断 + 白名单过滤的 _tool_schemas/_run_tool 单元行为。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent import Agent
from subagent_tools import SUBAGENT_TOOLSET
from tools import TOOL_SCHEMAS, TOOL_REGISTRY, TOOL_READ_ONLY, ToolContext, execute_tool, is_read_only


class ScriptedLLM:
    """按顺序逐轮吐剧本响应（父/子共用同一实例，消费顺序即调用顺序）。

    stop_at_child=True 时在子代理的第一次请求前置位 cancel——模拟"子代理
    刚开跑用户就点停止"，用于验证共享开关的取消传播。
    """

    def __init__(self, turns, stop_at_child=False):
        self.turns = list(turns)
        self.requests = []
        self.stop_at_child = stop_at_child

    def chat_stream(self, messages, tools=None, cancel=None):
        if self.stop_at_child and len(self.requests) == 1 and cancel is not None:
            cancel.set()
        self.requests.append({"messages": messages, "tools": tools})
        if not self.turns:
            raise AssertionError("剧本耗尽后仍在调用 LLM")
        turn = self.turns.pop(0)
        message = {"role": "assistant", "content": turn.get("content")}
        calls = [{"id": f"call_{i + 1}", "type": "function",
                  "function": {"name": c["name"],
                               "arguments": json.dumps(c.get("arguments") or {},
                                                       ensure_ascii=False)}}
                 for i, c in enumerate(turn.get("tool_calls") or [])]
        if calls:
            message["tool_calls"] = calls
        yield "message", message


def spawn_result(events):
    """从事件流里取 spawn_subagent 的 tool_result 信封（dict）。"""
    payloads = [p for k, p in events if k == "tool_result" and p["name"] == "spawn_subagent"]
    assert payloads, "事件流里没有 spawn_subagent 的结果"
    return json.loads(payloads[-1]["result"])


class TestWiring(unittest.TestCase):
    def test_schema_registry_and_read_only(self):
        names = [s["function"]["name"] for s in TOOL_SCHEMAS]
        self.assertIn("spawn_subagent", names)
        self.assertIn("spawn_subagent", TOOL_REGISTRY)
        self.assertFalse(is_read_only("spawn_subagent"))  # 刻意串行（v0）
        schema = TOOL_SCHEMAS[names.index("spawn_subagent")]["function"]
        self.assertEqual(schema["parameters"]["required"], ["task"])

    def test_bare_ctx_and_empty_task_rejected(self):
        env = json.loads(execute_tool("spawn_subagent", {"task": "x"}, ToolContext()))
        self.assertFalse(env["ok"])
        self.assertIn("未注入", env["error"])
        env = json.loads(execute_tool("spawn_subagent", {"task": "  "}, ToolContext()))
        self.assertFalse(env["ok"])
        self.assertIn("task 不能为空", env["error"])


class TestSubagentReplay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="subagent_test_")
        self.ws = Path(self._tmp.name)
        (self.ws / "a.txt").write_text("hello subagent", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, llm):
        return Agent(llm=llm, verbose=False, workspace=str(self.ws))

    def test_happy_path_report_and_isolation(self):
        llm = ScriptedLLM([
            # 父 r1：派子代理
            {"tool_calls": [{"name": "spawn_subagent",
                             "arguments": {"task": "目标：读 a.txt 说出内容；边界：只看 a.txt"}}]},
            # 子 r1：只读侦察
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            # 子 r2：结论
            {"content": "结论：a.txt 的内容是 hello subagent"},
            # 父 r2：基于回收结论作答
            {"content": "子代理回报：hello subagent"},
        ])
        agent = self._agent(llm)
        events = list(agent.run("派子代理看 a.txt"))
        env = spawn_result(events)
        self.assertTrue(env["ok"], env)
        self.assertIn("hello subagent", env["report"])
        self.assertEqual(env["rounds"], 2)
        self.assertIn("prompt_tokens", env["usage"])

        # 子代理请求隔离：专用系统提示 + 只读工具面（看不到 spawn_subagent/run_bash）
        child_req = llm.requests[1]
        self.assertTrue(child_req["messages"][0]["content"].startswith("你是只读侦察子代理"))
        child_tools = {s["function"]["name"] for s in child_req["tools"]}
        self.assertEqual(child_tools, set(SUBAGENT_TOOLSET))
        self.assertNotIn("spawn_subagent", child_tools)
        self.assertNotIn("run_bash", child_tools)

        # 父上下文不渗漏：父历史/轨迹里没有子代理的 read_file 轮次
        self.assertNotIn("read_file", json.dumps(agent.history))
        self.assertEqual([t.get("name") for t in agent.trace if t["type"] == "tool_call"],
                         ["spawn_subagent"])

    def test_child_write_and_recursion_rejected(self):
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"task": "乱来"}}]},
            # 子 r1：幻觉调用写工具 + 尝试递归派生（schema 里都看不见，纯执行层闸）
            {"tool_calls": [{"name": "write_file",
                             "arguments": {"path": "evil.txt", "content": "x"}},
                            {"name": "spawn_subagent", "arguments": {"task": "再派一个"}}]},
            {"content": "结论：我无权写文件，也不可再派子代理"},
            {"content": "收到"},
        ])
        agent = self._agent(llm)
        events = list(agent.run("派子代理"))
        env = spawn_result(events)
        self.assertTrue(env["ok"], env)
        self.assertFalse((self.ws / "evil.txt").exists())  # 写操作确实没发生
        child_second = json.dumps(llm.requests[2]["messages"], ensure_ascii=False)
        self.assertIn("无权使用工具 write_file", child_second)
        self.assertIn("无权使用工具 spawn_subagent", child_second)

    def test_cancel_propagation_shared_switch(self):
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"task": "侦察"}}]},
            {"content": "子代理的半截话"},  # 会被「已手动停止」收尾，不产出结论
        ], stop_at_child=True)
        agent = self._agent(llm)
        events = list(agent.run("派子代理"))
        env = spawn_result(events)
        self.assertFalse(env["ok"])
        self.assertIn("停止", env["error"])
        # 取消即时生效：子代理只烧了 1 次请求，父循环随后带 stopped 收场
        self.assertEqual(len(llm.requests), 2)
        done = [p for k, p in events if k == "done"][-1]
        self.assertTrue(done.get("stopped"))

    def test_preset_cancel_skips_child_entirely(self):
        llm = ScriptedLLM([])
        agent = self._agent(llm)
        agent.cancel_event = threading.Event()
        agent.cancel_event.set()
        env = json.loads(agent._spawn_subagent("任务"))
        self.assertFalse(env["ok"])
        self.assertIn("父回合已停止", env["error"])
        self.assertEqual(llm.requests, [])

    def test_rounds_exhausted_uses_wrap_up_round(self):
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"task": "侦察"}}]},
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            # 子的收尾轮（tools=None 的真实总结）
            {"content": "结论：侦查完毕，a.txt 无异常"},
            {"content": "子代理侦查完毕"},
        ])
        agent = self._agent(llm)
        with mock.patch("agent.SUBAGENT_MAX_ROUNDS", 2):
            events = list(agent.run("派子代理"))
        env = spawn_result(events)
        self.assertTrue(env["ok"], env)
        self.assertIn("侦查完毕", env["report"])
        self.assertEqual(env["rounds"], 3)  # 2 轮侦察 + 1 轮禁工具收尾
        self.assertIsNone(llm.requests[3]["tools"])  # 收尾轮禁工具

    def test_report_soft_cap_truncates(self):
        llm = ScriptedLLM([{"content": "R" * 200}])
        agent = self._agent(llm)
        with mock.patch("agent.SUBAGENT_REPORT_MAX_CHARS", 50):
            env = json.loads(agent._spawn_subagent("任务"))
        self.assertTrue(env["ok"])
        self.assertTrue(env["report"].startswith("RRRR"))
        self.assertIn("截断", env["report"])
        self.assertLess(len(env["report"]), 200)


class TestWhitelistFilter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="subagent_ws_")
        self.ws = Path(self._tmp.name)
        (self.ws / "a.txt").write_text("content", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_tool_schemas_filtered(self):
        class N:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "message", {"role": "assistant", "content": "ok"}
        agent = Agent(llm=N(), verbose=False, workspace=str(self.ws),
                      allowed_tools=("read_file",))
        self.assertEqual([s["function"]["name"] for s in agent._tool_schemas()], ["read_file"])

    def test_run_tool_gate_blocks_and_allows(self):
        class N:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "message", {"role": "assistant", "content": "ok"}
        agent = Agent(llm=N(), verbose=False, workspace=str(self.ws),
                      allowed_tools=SUBAGENT_TOOLSET)
        blocked = json.loads(agent._run_tool({"function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "x.txt", "content": "y"})}}))
        self.assertFalse(blocked["ok"])
        self.assertIn("无权使用工具 write_file", blocked["error"])
        self.assertFalse((self.ws / "x.txt").exists())
        allowed = json.loads(agent._run_tool({"function": {
            "name": "read_file", "arguments": json.dumps({"path": "a.txt"})}}))
        self.assertTrue(allowed["ok"], allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
