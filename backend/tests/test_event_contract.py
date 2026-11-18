"""事件契约（backend/contracts/events.py）的单测。

契约层的价值全在"能不能真的抓住漂移"，所以这里除了测校验函数本身，
更重要的一条是 **contract_matches_real_events**：真实跑一个回合，
把产出的事件逐条过契约——字段少一个、多一个都会红。

这也顺带回答了"docs/protocol.md 和代码是否还一致"：文档写在 EVENTS 里，
代码产出要过它。
"""

import json
import os
import pathlib
import unittest
from unittest import mock

from contracts import events as c


class ValidateTest(unittest.TestCase):
    def test_clean_event_passes(self):
        self.assertEqual(c.validate({"type": "round", "round": 1, "seq": 3}), [])

    def test_missing_required_field_caught(self):
        # answer_delta 少了 mid：前端会归并到错误气泡，是最典型的漂移
        problems = c.validate({"type": "answer_delta", "text": "hi"})
        self.assertTrue(any("mid" in p for p in problems), problems)

    def test_undeclared_field_caught(self):
        problems = c.validate({"type": "round", "round": 1, "roundNo": 1})
        self.assertTrue(any("未声明" in p for p in problems), problems)

    def test_unknown_type_reported(self):
        problems = c.validate({"type": "made_up_event"})
        self.assertTrue(problems and "未知事件类型" in problems[0])

    def test_non_dict_and_missing_type(self):
        self.assertTrue(c.validate(["not", "a", "dict"]))
        self.assertTrue(c.validate({"round": 1}))

    def test_seq_and_ts_are_allowed_on_every_event(self):
        """seq/ts 由传输层附加，不属于任何单一事件的声明字段。"""
        self.assertEqual(c.validate({"type": "tool_call", "name": "x",
                                     "arguments": "{}", "seq": 9, "ts": 1.0}), [])


class StrictModeTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("EVENT_CONTRACT_STRICT", None)
        self.addCleanup(lambda: os.environ.pop("EVENT_CONTRACT_STRICT", None))

    def test_default_off(self):
        self.assertEqual(c.strict_level(), 0)
        self.assertTrue(c.check({"type": "answer_delta", "text": "x"}), "默认不校验，一律放行")

    def test_level_one_warns_only(self):
        with mock.patch.dict(os.environ, {"EVENT_CONTRACT_STRICT": "1"}):
            self.assertFalse(c.check({"type": "answer_delta", "text": "x"}))

    def test_level_two_raises(self):
        with mock.patch.dict(os.environ, {"EVENT_CONTRACT_STRICT": "2"}):
            with self.assertRaises(c.ContractViolation):
                c.check({"type": "answer_delta", "text": "x"})

    def test_unknown_type_is_info_not_violation(self):
        """未知类型在 strict=1 下算 info，不算违约——前后端版本错开是常态。"""
        with mock.patch.dict(os.environ, {"EVENT_CONTRACT_STRICT": "1"}):
            self.assertTrue(c.check({"type": "brand_new_event"}))


class SchemaExportTest(unittest.TestCase):
    def test_schema_file_is_in_sync(self):
        """前端那份 events.schema.json 必须与后端一致，防止手写第二份后漂移。

        改了 EVENTS 忘了重新导出 → 这条测试直接红，并给出重新生成的命令。
        """
        path = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "events.schema.json"
        self.assertTrue(path.exists(), "缺少 frontend/events.schema.json")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, c.schema(),
                         "schema 已过期：cd backend && python3 -c \"import json,sys;"
                         "sys.path.insert(0,'.');from contracts import events;"
                         "open('../frontend/events.schema.json','w').write("
                         "json.dumps(events.schema(),ensure_ascii=False,indent=2)+'\\n')\"")


class RealEventsConformTest(unittest.TestCase):
    """真实跑一个回合，逐条验证产出的事件符合契约。

    这是最有价值的一条：它同时防止"后端少发字段"和"文档/契约忘了更新"。
    """

    def setUp(self):
        os.environ.pop("EVENT_CONTRACT_STRICT", None)
        self.addCleanup(lambda: os.environ.pop("EVENT_CONTRACT_STRICT", None))

    @staticmethod
    def _agent():
        import shutil
        import tempfile
        from agent import Agent

        class _LLM:
            def chat_stream(self, messages, tools=None, cancel=None):
                yield "delta", "好的"
                yield "message", {"role": "assistant", "content": "好的"}

        ws = tempfile.mkdtemp(prefix="contract_ws_")
        return Agent(llm=_LLM(), verbose=False, workspace=ws), ws

    def test_round_events_conform(self):
        """校验的是「发布层形状」而不是 Agent 内部 yield。

        Agent.run 产出的是 (kind, payload)，其中 answer_delta 不带 mid ——
        mid 由 services/turn.py 发布时补（seg_mid 归并气泡）。所以这里按
        turn.py 的拼装规则还原后再校验，测的才是前端真正收到的东西。
        """
        import shutil
        agent, ws = self._agent()
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        problems = []
        seg_mid = "seg123456789"
        for kind, payload in agent.run("说句话"):
            if kind == "round":
                evt = {"type": "round", "mid": seg_mid, **payload}
            elif kind in ("answer_delta", "done"):
                evt = {"type": kind, "mid": seg_mid, **payload}
            elif kind == "reasoning_delta":
                evt = {"type": kind, **payload}   # 刻意不带 mid
            else:
                evt = {"type": kind, **payload}
            problems.extend(f"{kind}: {p}" for p in c.validate(evt))
        self.assertEqual(problems, [], "真实事件与契约不符：\n" + "\n".join(problems))


class PublishHookTest(unittest.TestCase):
    def test_publish_calls_contract_check(self):
        """publish 必须过契约（否则前端拿到的是没人把关的形状）。"""
        import events as ev
        from queue import Queue

        publisher = ev.SessionEvents()
        with mock.patch.object(c, "check", return_value=True) as m:
            publisher.publish({"type": "round", "round": 1})
        self.assertTrue(m.called, "publish 没有再经过契约校验")

    def test_publish_survives_violation(self):
        """给了不合规事件也不能中断事件流：用户不该因为一个字段 typo 看到卡住。"""
        import events as ev

        publisher = ev.SessionEvents()
        seq = publisher.publish({"type": "answer_delta", "text": "缺 mid"})
        self.assertGreater(seq, 0)


if __name__ == "__main__":
    unittest.main()
