"""
回放式评测 harness（cd backend && python3 -m unittest tests.test_replay_eval -v）
==================================================================================

借鉴 DeepSeek V3.2 训练侧的「任务合成 + 轨迹回放」与 dsh 仓库 benchmarks/ 的
思路，把 manual/ 的手测脚本升级成 CI 可跑的回归网：剧本（fixtures/*.json）
描述一段真实会话的助手动作序列（tool_calls / 最终回答），回放器把它逐轮喂给
Agent 循环，断言工具调用序列、每步结果信封、最终回答与工作区的真实状态。

分工与工作流：
  真实会话 → manual/record_trajectory.py 从库导出剧本草稿 → 人工修整（补
  workspace_files 与 expect）→ 存进 fixtures/ → 本测试回放断言。
  改动 agent.py 循环（调度/回填/压缩/收尾）之后，先跑这一套再跑全量——
  剧本就是"这个 agent 该怎么干活"的可执行规范。

fixture 字段：
  workspace_files   开场工作区文件 {路径: 内容}（临时目录，逐字节写入）
  user              首条用户消息
  turns[]           按顺序的 LLM 响应；tool_calls 的 arguments 是 dict（正常）
                    或 arguments_raw 是字符串（可故意写坏，验证自修路径）
  expect            断言词典：tool_sequence / tool_results_ok /
                    first_result_has_schema / answer_contains / file_contains
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import Agent

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ReplayLLM:
    """剧本回放客户端：按顺序逐轮吐出剧本响应，同时记录收到的请求供断言。

    剧本耗尽后仍被调用 = agent 的行为偏离剧本（多出来的轮次），当场报错——
    这本身就是一条断言：剧本的最终回合必须是回答，循环必须就此收场。
    """

    def __init__(self, turns: list):
        self.turns = [dict(t) for t in turns]
        self.requests: list[dict] = []

    def chat_stream(self, messages, tools=None, cancel=None):
        self.requests.append({"messages": messages, "tools": tools})
        if not self.turns:
            raise AssertionError("剧本耗尽后 agent 仍在调用 LLM（行为偏离剧本）")
        turn = self.turns.pop(0)
        message: dict = {"role": "assistant",
                         "content": turn.get("content") if "content" in turn else None}
        calls = []
        for i, c in enumerate(turn.get("tool_calls") or []):
            if "arguments_raw" in c:
                args = c["arguments_raw"]  # 故意写坏的 JSON：验证错误回传与自修
            else:
                args = json.dumps(c.get("arguments") or {}, ensure_ascii=False)
            calls.append({"id": f"call_{i + 1}", "type": "function",
                          "function": {"name": c["name"], "arguments": args}})
        if calls:
            message["tool_calls"] = calls
        yield "message", message


def run_scenario(fixture: dict):
    """按剧本搭临时工作区、驱动完整 agent 循环，返回 (events, agent, llm, ws)。"""
    ws = Path(tempfile.mkdtemp(prefix="replay_eval_"))
    for name, text in (fixture.get("workspace_files") or {}).items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    llm = ReplayLLM(fixture["turns"])
    agent = Agent(llm=llm, verbose=False, workspace=str(ws))
    events = list(agent.run(fixture["user"]))
    return events, agent, llm, ws


class TestReplayScenarios(unittest.TestCase):
    """遍历 fixtures/*.json 逐剧本回放。新增回归场景 = 往目录里放一个 json。"""

    def test_every_fixture_replays_to_expectation(self):
        files = sorted(FIXTURES.glob("*.json"))
        self.assertTrue(files, "fixtures 目录为空——评测网失效")
        for f in files:
            with self.subTest(fixture=f.name):
                fixture = json.loads(f.read_text(encoding="utf-8"))
                self._assert_scenario(fixture)

    # ------------------------------------------------------------------

    def _assert_scenario(self, fx: dict):
        expect = fx.get("expect") or {}
        events, agent, llm, ws = run_scenario(fx)

        by_type = {}
        for kind, payload in events:
            by_type.setdefault(kind, []).append(payload)

        # 工具调用序列与每步结果
        tools = [p["name"] for p in by_type.get("tool_call", [])]
        if "tool_sequence" in expect:
            self.assertEqual(tools, expect["tool_sequence"], fx["name"])
        results = by_type.get("tool_result", [])
        if "tool_results_ok" in expect:
            self.assertEqual(len(results), len(expect["tool_results_ok"]), fx["name"])
            for r, ok in zip(results, expect["tool_results_ok"]):
                envelope = json.loads(r["result"])
                self.assertEqual(envelope.get("ok"), ok,
                                 f"{fx['name']}: {r['name']} → {r['result'][:200]}")
        if expect.get("first_result_has_schema"):
            self.assertTrue(results, fx["name"])
            envelope = json.loads(results[0]["result"])
            self.assertFalse(envelope.get("ok"))
            self.assertIn("schema", envelope)  # 参数写坏 → 信封必须带期望定义

        # 最终回答
        done = by_type.get("done", [{}])[-1]
        if "answer_contains" in expect:
            self.assertIn(expect["answer_contains"], done.get("answer") or "", fx["name"])
        self.assertFalse(done.get("stopped"), fx["name"])  # 剧本不该走到被停止

        # 工作区真实状态（改动必须落盘，不是嘴上说改了）
        for name, needle in (expect.get("file_contains") or {}).items():
            self.assertIn(needle, (ws / name).read_text(encoding="utf-8"), fx["name"])

        # 历史骨架健全：tool 消息与 assistant.tool_calls 一一配对、无悬空
        tool_ids = [c["id"] for m in agent.history if m.get("tool_calls") for c in m["tool_calls"]]
        result_ids = [m["tool_call_id"] for m in agent.history if m["role"] == "tool"]
        self.assertEqual(tool_ids, result_ids, fx["name"])

        # 剧本的最后一回合被消费掉（循环在回答处收场，没有多余轮次）
        self.assertEqual(llm.turns, [], fx["name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
