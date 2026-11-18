"""backend/turn_state.py（回合状态机，内存态）的单测。

与 test_turn_state.py 区分：那个测的是数据库里的 session_turns 台账；
这个测的是 _run 循环里那个内存状态机。

首版状态机是「只断言告警、不拦截」的观测设施，所以测试重点是：
 1. 合法路径全部放行（装了它不能把正常运行卡住）；
 2. 非法迁移默认只告警、不抛错、状态不动（不能让界面停在假状态）；
 3. strict 模式下才抛 IllegalTransition；
 4. 终态不可离开（避免"结束后又跑起来"）；
 5. 并发下告警计数不丢（否则"零告警"的结论不可信）。
"""

import os
import threading
import unittest
from unittest import mock

import turn_state as ts


class TurnStateMachineTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AGENT_STATE_STRICT", None)
        self.addCleanup(lambda: os.environ.pop("AGENT_STATE_STRICT", None))

    def test_normal_round_path_is_allowed(self):
        s = ts.TurnState()
        self.assertTrue(s.to(ts.REQUESTING))
        self.assertTrue(s.to(ts.STREAMING))
        self.assertTrue(s.to(ts.TOOL_RUNNING))
        self.assertTrue(s.to(ts.REQUESTING))   # 工具跑完再请求下一轮
        self.assertTrue(s.to(ts.STREAMING))
        self.assertTrue(s.to(ts.DONE))
        self.assertTrue(s.is_terminal)

    def test_wrapping_up_path_is_allowed(self):
        s = ts.TurnState()
        s.to(ts.REQUESTING); s.to(ts.STREAMING)
        self.assertTrue(s.to(ts.WRAPPING_UP))
        self.assertTrue(s.to(ts.STREAMING))    # 收尾轮复用同一份流式消费
        self.assertTrue(s.to(ts.DONE))

    def test_cancel_path_is_allowed(self):
        s = ts.TurnState()
        s.to(ts.REQUESTING)
        self.assertTrue(s.to(ts.CANCELLING))
        self.assertTrue(s.to(ts.DONE))

    def test_illegal_transition_warns_but_does_not_move(self):
        s = ts.TurnState()
        s.to(ts.REQUESTING)
        self.assertFalse(s.to(ts.WRAPPING_UP, "不该从 requesting 直接收尾"))
        self.assertEqual(s.state, ts.REQUESTING)
        self.assertEqual(s.illegal_count(), 1)

    def test_strict_mode_raises(self):
        os.environ["AGENT_STATE_STRICT"] = "1"
        s = ts.TurnState()
        s.to(ts.REQUESTING)
        with self.assertRaises(ts.IllegalTransition):
            s.to(ts.WRAPPING_UP)

    def test_terminal_is_final(self):
        for terminal in (ts.DONE, ts.ERROR):
            s = ts.TurnState()
            s.to(ts.REQUESTING)
            s.to(terminal)
            self.assertTrue(s.is_terminal)
            self.assertFalse(s.to(ts.REQUESTING, "结束后不能再请求"))
            self.assertEqual(s.state, terminal)

    def test_idle_can_go_straight_to_terminal(self):
        """空回合（一开始就被取消 / 立刻报错）不应被记成非法。"""
        s = ts.TurnState()
        self.assertTrue(s.to(ts.ERROR))

    def test_history_records_only_legal_hops(self):
        s = ts.TurnState()
        s.to(ts.REQUESTING); s.to(ts.STREAMING); s.to(ts.DONE)
        self.assertEqual(s.history(), [(ts.IDLE, ts.REQUESTING),
                                       (ts.REQUESTING, ts.STREAMING),
                                       (ts.STREAMING, ts.DONE)])

    def test_concurrent_transitions_do_not_lose_warnings(self):
        """并发下告警计数不能丢——否则"零告警"的结论不可信。"""
        s = ts.TurnState()
        s.to(ts.REQUESTING)
        N = 50

        def worker():
            for _ in range(N):
                s.to(ts.WRAPPING_UP)   # 全部非法

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(s.illegal_count(), 8 * N)
        self.assertEqual(s.state, ts.REQUESTING)

    def test_strict_mode_env_parsing(self):
        with mock.patch.dict(os.environ, {"AGENT_STATE_STRICT": "true"}):
            self.assertTrue(ts.strict_mode())
        with mock.patch.dict(os.environ, {"AGENT_STATE_STRICT": "0"}):
            self.assertFalse(ts.strict_mode())


class _OneShotLLM:
    """假模型：流式吐一小段文字后给出最终 assistant 消息（不调工具）。"""

    def __init__(self, text="完成了"):
        self.text = text

    def chat_stream(self, messages, tools=None, cancel=None):
        yield "delta", self.text
        yield "message", {"role": "assistant", "content": self.text}


class AgentTurnStateIntegrationTest(unittest.TestCase):
    """真实 Agent.run() 跑一个完整回合：状态必须走到 DONE 且零非法迁移。

    这是「只告警不拦截」模式下最有价值的回归：一旦接线点插错位置
    （比如把 STREAMING 写在 tool 执行之后），这里会立刻冒出非法迁移记录。
    跑法：cd backend && python3 -m unittest tests.test_turn_machine -v
    """

    def setUp(self):
        os.environ.pop("AGENT_STATE_STRICT", None)
        self.addCleanup(lambda: os.environ.pop("AGENT_STATE_STRICT", None))

    def _make_agent(self):
        import shutil
        import tempfile
        from agent import Agent
        ws = tempfile.mkdtemp(prefix="turn_machine_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        return Agent(llm=_OneShotLLM(), verbose=False, workspace=ws)

    def test_full_turn_reaches_done_without_illegal_transitions(self):
        agent = self._make_agent()
        kinds = [k for k, _ in agent.run("随便问一句")]
        self.assertIn("done", kinds, "正常回合必须产出 done")
        self.assertEqual(agent.turn_state.state, ts.DONE)
        self.assertTrue(agent.turn_state.is_terminal)
        self.assertEqual(agent.turn_state.illegal_count(), 0,
                         f"非法迁移序列：{agent.turn_state.history()}")
        # 迁移链必须包含：请求 → 流式 → 终态
        hops = [f"{a}→{b}" for a, b in agent.turn_state.history()]
        self.assertIn("idle→requesting", hops)
        self.assertIn("requesting→streaming", hops)
        self.assertTrue(hops[-1].endswith("done"), hops)

    def test_exception_mid_turn_lands_in_error_state(self):
        """中途抛异常（生成器被关掉/模型炸了）：finally 兜底必须落在 ERROR。"""
        agent = self._make_agent()
        agent.turn_state.to(ts.REQUESTING)   # 模拟已经进入请求阶段

        def boom(u, um):
            raise RuntimeError("模拟中途炸了")
            yield  # pragma: no cover

        agent._run = boom
        with self.assertRaises(RuntimeError):
            list(agent.run("问"))
        self.assertEqual(agent.turn_state.state, ts.ERROR)
        self.assertTrue(agent.turn_state.is_terminal)


if __name__ == "__main__":
    unittest.main()
