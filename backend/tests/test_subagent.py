"""
子代理测试（cd backend && python3 -m unittest tests.test_subagent -v）
================================================================================

覆盖 spawn_subagent 读侧扇出的关键面：
  接线 —— schema/注册/只读标记（串行组内自带并行）、参数校验；
  回放 —— 父派出 → 子只读侦察 → 结论回填，父子上下文互不渗漏；
  闸门 —— 子代理写操作与递归派生被白名单拒绝；用户自定义 deny 规则继承生效；
  取消 —— 父「停止」共享开关，子代理即时收场、不烧多余 LLM 调用；
  并行 —— Barrier 证明多任务真并发、结果按提交顺序回填、单任务失败不连坐；
  收尾 —— 轮数耗尽走禁工具收尾轮；报告超长软闸截断。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent import Agent
from permissions import PermissionGate
from subagent_tools import SUBAGENT_MAX_PARALLEL, SUBAGENT_TOOLSET
from tools import TOOL_SCHEMAS, TOOL_REGISTRY, ToolContext, execute_tool, is_read_only


def _turn_to_message(turn):
    message = {"role": "assistant", "content": turn.get("content")}
    calls = [{"id": f"call_{i + 1}", "type": "function",
              "function": {"name": c["name"],
                           "arguments": json.dumps(c.get("arguments") or {},
                                                   ensure_ascii=False)}}
             for i, c in enumerate(turn.get("tool_calls") or [])]
    if calls:
        message["tool_calls"] = calls
    return message


class ScriptedLLM:
    """按顺序逐轮吐剧本响应（父/子共用同一实例，串行消费顺序即调用顺序）。

    stop_at_child=True 时在子代理的第一次请求前置位 cancel——模拟"子代理
    刚开跑用户就点停止"，用于验证共享开关的取消传播。
    """

    def __init__(self, turns, stop_at_child=False):
        self.turns = list(turns)
        self.requests = []
        self.stop_at_child = stop_at_child

    def chat_stream(self, messages, tools=None, cancel=None):
        is_child = (messages[0].get("content") or "").startswith("你是只读侦察子代理")
        if self.stop_at_child and is_child and len(self.requests) == 1 and cancel is not None:
            cancel.set()
        self.requests.append({"messages": messages, "tools": tools, "child": is_child})
        if not self.turns:
            raise AssertionError("剧本耗尽后仍在调用 LLM")
        return iter([("message", _turn_to_message(self.turns.pop(0)))])


class RoutingLLM:
    """并行扇出测试用：父回合走剧本队列；子代理请求按 system 提示识别、按
    task 文本路由到各自的子剧本。多个子代理并发调用，故状态读写持锁。

    child_first_call(task, cancel)：子代理首轮请求前调用（并发汇合点/延迟/
    注入停止都挂这里）；child_plan(task)：返回该任务的子剧本（默认读文件→结论）。
    """

    def __init__(self, parent_turns, child_first_call=None, child_plan=None):
        self.parent_turns = list(parent_turns)
        self.child_first_call = child_first_call
        self.child_plan = child_plan or (
            lambda task: [{"tool_calls": [{"name": "read_file",
                                           "arguments": {"path": "a.txt"}}]},
                          {"content": f"结论：{task} → a.txt 无异常"}])
        self.child_steps: dict[str, int] = {}
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def chat_stream(self, messages, tools=None, cancel=None):
        is_child = (messages[0].get("content") or "").startswith("你是只读侦察子代理")
        if is_child:
            task = messages[1]["content"]
            with self.lock:
                step = self.child_steps.get(task, 0)
                self.child_steps[task] = step + 1
            if step == 0 and self.child_first_call is not None:
                self.child_first_call(task, cancel)
            plan = self.child_plan(task)
            if step >= len(plan):
                raise AssertionError(f"子剧本耗尽（task={task[:40]}）")
            turn = plan[step]
        else:
            if not self.parent_turns:
                raise AssertionError("父剧本耗尽后仍在调用 LLM")
            turn = self.parent_turns.pop(0)
        with self.lock:
            self.requests.append({"messages": messages, "tools": tools, "child": is_child})
        return iter([("message", _turn_to_message(turn))])


def spawn_envelope(events):
    """从事件流里取 spawn_subagent 的 tool_result 信封（dict）。"""
    payloads = [p for k, p in events if k == "tool_result" and p["name"] == "spawn_subagent"]
    assert payloads, "事件流里没有 spawn_subagent 的结果"
    return json.loads(payloads[-1]["result"])


class TestWiring(unittest.TestCase):
    def test_schema_registry_and_read_only(self):
        names = [s["function"]["name"] for s in TOOL_SCHEMAS]
        self.assertIn("spawn_subagent", names)
        self.assertIn("spawn_subagent", TOOL_REGISTRY)
        self.assertFalse(is_read_only("spawn_subagent"))  # 独占串行组，并行封装在信封内
        schema = TOOL_SCHEMAS[names.index("spawn_subagent")]["function"]
        self.assertEqual(schema["parameters"]["required"], ["tasks"])
        self.assertEqual(schema["parameters"]["properties"]["tasks"]["maxItems"],
                         SUBAGENT_MAX_PARALLEL)

    def test_argument_validation(self):
        bare = ToolContext()
        cases = [
            ({"tasks": []}, "tasks 必须是非空数组"),
            ({}, "tasks 必须是非空数组"),
            ({"tasks": ["", "  "]}, "tasks 里存在空任务"),
            ({"tasks": ["a"] * (SUBAGENT_MAX_PARALLEL + 1)}, "一次最多并行派出"),
        ]
        for arguments, needle in cases:
            env = json.loads(execute_tool("spawn_subagent", arguments, bare))
            self.assertFalse(env["ok"], arguments)
            self.assertIn(needle, env["error"])
        env = json.loads(execute_tool("spawn_subagent", {"tasks": ["x"]}, bare))
        self.assertFalse(env["ok"])
        self.assertIn("未注入", env["error"])


class TestSubagentReplay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="subagent_test_")
        self.ws = Path(self._tmp.name)
        (self.ws / "a.txt").write_text("hello subagent", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, llm, **kw):
        return Agent(llm=llm, verbose=False, workspace=str(self.ws), **kw)

    def test_happy_path_report_and_isolation(self):
        llm = ScriptedLLM([
            # 父 r1：派子代理（单任务数组）
            {"tool_calls": [{"name": "spawn_subagent",
                             "arguments": {"tasks": ["目标：读 a.txt 说出内容；边界：只看 a.txt"]}}]},
            # 子 r1：只读侦察；子 r2：结论
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            {"content": "结论：a.txt 的内容是 hello subagent"},
            # 父 r2：基于回收结论作答
            {"content": "子代理回报：hello subagent"},
        ])
        agent = self._agent(llm)
        events = list(agent.run("派子代理看 a.txt"))
        env = spawn_envelope(events)
        self.assertTrue(env["ok"], env)
        self.assertEqual(len(env["results"]), 1)
        item = env["results"][0]
        self.assertTrue(item["ok"], item)
        self.assertIn("hello subagent", item["report"])
        self.assertEqual(item["rounds"], 2)
        self.assertIn("prompt_tokens", item["usage"])

        # 子代理请求隔离：专用系统提示 + 只读工具面（看不到 spawn_subagent/run_bash）
        child_req = llm.requests[1]
        self.assertTrue(child_req["child"])
        self.assertTrue(child_req["messages"][0]["content"].startswith("你是只读侦察子代理"))
        child_tools = {s["function"]["name"] for s in child_req["tools"]}
        self.assertEqual(child_tools, set(SUBAGENT_TOOLSET))

        # 父上下文不渗漏：父历史/轨迹里没有子代理的 read_file 轮次
        self.assertNotIn("read_file", json.dumps(agent.history))
        self.assertEqual([t.get("name") for t in agent.trace if t["type"] == "tool_call"],
                         ["spawn_subagent"])

    def test_child_write_and_recursion_rejected(self):
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"tasks": ["乱来"]}}]},
            # 子 r1：幻觉调用写工具 + 尝试递归派生（schema 里都看不见，纯执行层闸）
            {"tool_calls": [{"name": "write_file",
                             "arguments": {"path": "evil.txt", "content": "x"}},
                            {"name": "spawn_subagent", "arguments": {"tasks": ["再派一个"]}}]},
            {"content": "结论：我无权写文件，也不可再派子代理"},
            {"content": "收到"},
        ])
        agent = self._agent(llm)
        events = list(agent.run("派子代理"))
        item = spawn_envelope(events)["results"][0]
        self.assertTrue(item["ok"], item)
        self.assertFalse((self.ws / "evil.txt").exists())  # 写操作确实没发生
        child_second = json.dumps(llm.requests[2]["messages"], ensure_ascii=False)
        self.assertIn("无权使用工具 write_file", child_second)
        self.assertIn("无权使用工具 spawn_subagent", child_second)

    def test_user_deny_rule_inherited(self):
        """用户自定义 deny 规则必须对子代理生效——否则子代理成了绕过禁令的
        旁路（父代理被禁读的路径子代理能读）。"""
        (self.ws / "secret.txt").write_text("topsecret", encoding="utf-8")
        gate = PermissionGate(self.ws, ask_timeout=0.0, user_rules_loader=lambda: [
            {"tool": "read_file", "decision": "deny", "reason": "敏感目录禁止读取"}])
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent",
                             "arguments": {"tasks": ["目标：读 secret.txt"]}}]},
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "secret.txt"}}]},
            {"content": "结论：读取被拒绝"},
            {"content": "收到"},
        ])
        agent = self._agent(llm, permission_gate=gate)
        events = list(agent.run("派子代理"))
        item = spawn_envelope(events)["results"][0]
        self.assertTrue(item["ok"], item)
        child_second = json.dumps(llm.requests[2]["messages"], ensure_ascii=False)
        self.assertIn("敏感目录禁止读取", child_second)
        self.assertNotIn("topsecret", child_second)  # 内容确实没被读到

    def test_cancel_propagation_shared_switch(self):
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"tasks": ["侦察"]}}]},
            {"content": "子代理的半截话"},  # 会被「已手动停止」收尾，不产出结论
        ], stop_at_child=True)
        agent = self._agent(llm)
        events = list(agent.run("派子代理"))
        item = spawn_envelope(events)["results"][0]
        self.assertFalse(item["ok"])
        self.assertIn("停止", item["error"])
        # 取消即时生效：子代理只烧了 1 次请求，父循环随后带 stopped 收场
        self.assertEqual(len(llm.requests), 2)
        done = [p for k, p in events if k == "done"][-1]
        self.assertTrue(done.get("stopped"))

    def test_preset_cancel_skips_child_entirely(self):
        llm = ScriptedLLM([])
        agent = self._agent(llm)
        agent.cancel_event = threading.Event()
        agent.cancel_event.set()
        env = json.loads(agent._spawn_subagent(["任务"]))
        self.assertFalse(env["ok"])
        self.assertIn("父回合已停止", env["error"])
        self.assertEqual(llm.requests, [])

    def test_rounds_exhausted_uses_wrap_up_round(self):
        llm = ScriptedLLM([
            {"tool_calls": [{"name": "spawn_subagent", "arguments": {"tasks": ["侦察"]}}]},
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
            # 子的收尾轮（tools=None 的真实总结）
            {"content": "结论：侦查完毕，a.txt 无异常"},
            {"content": "子代理侦查完毕"},
        ])
        agent = self._agent(llm)
        with mock.patch("agent.SUBAGENT_MAX_ROUNDS", 2):
            events = list(agent.run("派子代理"))
        item = spawn_envelope(events)["results"][0]
        self.assertTrue(item["ok"], item)
        self.assertIn("侦查完毕", item["report"])
        self.assertEqual(item["rounds"], 3)  # 2 轮侦察 + 1 轮禁工具收尾
        self.assertIsNone(llm.requests[3]["tools"])  # 收尾轮禁工具

    def test_report_soft_cap_truncates(self):
        llm = ScriptedLLM([{"content": "R" * 200}])
        agent = self._agent(llm)
        with mock.patch("agent.SUBAGENT_REPORT_MAX_CHARS", 50):
            env = json.loads(agent._spawn_subagent(["任务"]))
        item = env["results"][0]
        self.assertTrue(item["ok"])
        self.assertTrue(item["report"].startswith("RRRR"))
        self.assertIn("截断", item["report"])
        self.assertLess(len(item["report"]), 200)


class TestParallelFanout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="subagent_par_")
        self.ws = Path(self._tmp.name)
        (self.ws / "a.txt").write_text("content", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, llm):
        agent = Agent(llm=llm, verbose=False, workspace=str(self.ws))
        events = list(agent.run("并行派子代理"))
        return spawn_envelope(events), agent

    def test_parallel_tasks_actually_overlap(self):
        """确定性并发证明：两个子代理在各自首轮请求处会合于 Barrier——串行
        执行永远等不到对方、超时炸成 error 条目，只有真并发才能双双通过。"""
        barrier = threading.Barrier(2, timeout=15)
        llm = RoutingLLM(
            parent_turns=[
                {"tool_calls": [{"name": "spawn_subagent",
                                 "arguments": {"tasks": ["T1", "T2"]}}]},
                {"content": "两路都回来了"},
            ],
            child_first_call=lambda task, cancel: barrier.wait())
        env, _ = self._run(llm)
        self.assertTrue(env["ok"], env)
        self.assertTrue(all(r["ok"] for r in env["results"]), env)
        self.assertEqual([r["task"] for r in env["results"]], ["T1", "T2"])

    def test_results_backfilled_in_task_order(self):
        """慢任务先提交也按 tasks 顺序回填（与工具并行组同一条回填不变式）。"""
        delays = {"T慢": 0.4, "T快": 0.05}
        llm = RoutingLLM(
            parent_turns=[
                {"tool_calls": [{"name": "spawn_subagent",
                                 "arguments": {"tasks": ["T慢", "T快"]}}]},
                {"content": "都回来了"},
            ],
            child_first_call=lambda task, cancel: time.sleep(delays[task]))
        env, _ = self._run(llm)
        self.assertEqual([r["task"] for r in env["results"]], ["T慢", "T快"])
        self.assertTrue(all(r["ok"] for r in env["results"]), env)
        self.assertLess(sum(delays.values()), 1.0)  # 延迟本身不构成串行假设

    def test_single_task_failure_does_not_contaminate_batch(self):
        """一个子代理的剧本异常只折损自己的条目，同批其它任务照常回收。"""
        def plan(task):
            if task == "BAD":
                raise RuntimeError("子代理 LLM 连接中断")
            return [{"content": f"结论：{task} 完成"}]
        llm = RoutingLLM(
            parent_turns=[
                {"tool_calls": [{"name": "spawn_subagent",
                                 "arguments": {"tasks": ["BAD", "GOOD"]}}]},
                {"content": "收到"},
            ],
            child_plan=plan)
        env, _ = self._run(llm)
        self.assertTrue(env["ok"], env)  # 扇出机制本身成功，逐条看 ok
        by_task = {r["task"]: r for r in env["results"]}
        self.assertFalse(by_task["BAD"]["ok"])
        self.assertIn("子代理执行失败", by_task["BAD"]["error"])
        self.assertTrue(by_task["GOOD"]["ok"])
        self.assertIn("GOOD 完成", by_task["GOOD"]["report"])


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
