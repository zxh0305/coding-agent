"""
收尾轮 / 防失控提醒 / 合成消息生命周期的单元测试
（cd backend && python3 -m unittest test_wrapup -v）
================================================================================

覆盖「轮数上限 = 强制杀死回合」改造为「防失控提醒 + 到限优雅收尾」的三块核心：

1. 收尾轮（_wrap_up_round）：跑满 max_rounds 后注入合成 user 指令、以 tools=None
   再请求一轮——请求末尾是剥掉 _synthetic 的合成消息、不再带工具清单；done 带
   真实总结与 stopped_reason="max_rounds"；收尾途中被停止 → 半截文字 +
   stopped=True；收尾轮炸 → RuntimeError 原样上抛（worker 错误路径收尾）；
2. 合成消息（_synthetic）生命周期：发给模型保留内容、剥掉标记；落库跳过、
   不进指纹账本、二次保存零写入（重启恢复后无残留，DB 仍是时间线唯一真相）；
3. 防失控提醒（_maybe_remind）：同一工具+相同参数连续 3 次注入循环提醒且同段
   只提醒一次、换参数重置后可再次触发、每回合总预算封顶（MAX_TURN_REMINDERS）；
   预算提醒在 max_rounds-10 / max_rounds-4 轮各一次且受总预算约束。

另测 Anthropic 适配器把「tool 结果之后紧跟的合成 user 消息」并进同一个
user turn（tool_result 块在前、text 块在后，不是两条连续 user 消息）。
全部跑在假 LLM 与临时库上，不碰网络。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import db
from agent import MAX_TURN_REMINDERS, WRAP_UP_INSTRUCTION, Agent
from llm_client import AnthropicMessagesClient


# ---------- 消息与调用构造 ----------

def user(text, **extra):
    return {"role": "user", "content": text, **extra}


def asst(text):
    return {"role": "assistant", "content": text}


def call(cid, name, **args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def tool_result(text, call_id="c1"):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def synthetic(text):
    """运行时构造的合成消息（收尾指令 / 循环提醒）的形状。"""
    return {"role": "user", "_synthetic": True, "content": text}


class RecordingLLM:
    """按剧本逐轮吐消息的假 LLM，并记录每次请求（messages/tools/cancel）。

    剧本元素：dict → 该轮的 assistant 消息；Exception 子类实例 → 该轮抛出。
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def chat_stream(self, messages, tools=None, cancel=None):
        self.requests.append({"messages": messages, "tools": tools, "cancel": cancel})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        yield "message", item


class WrapupTestBase(unittest.TestCase):
    def make_agent(self, llm=None, **kw) -> Agent:
        # 独立临时工作区：Agent.__init__ 会 prepare_workspace，别让它碰项目目录
        ws = tempfile.mkdtemp(prefix="wrapup_test_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        return Agent(llm=llm or RecordingLLM([]), verbose=False, workspace=ws, **kw)


# ---------------------------------------------------------------------------
# 一、收尾轮
# ---------------------------------------------------------------------------

class TestWrapUpRound(WrapupTestBase):

    def test_max_rounds_triggers_wrap_up_summary(self):
        """三轮工具后：第 4 轮是收尾轮——请求 tools=None、末尾是剥掉 _synthetic
        的合成指令；done 是真实总结 + stopped_reason，不再是"强制停止"文案。"""
        summary = "总结：方案已读取，前两步完成，第三步待办"
        llm = RecordingLLM([
            tool_call_message(call("c1", "read_file", path="plan.md")),
            tool_call_message(call("c2", "grep", pattern="TODO")),
            tool_call_message(call("c3", "list_dir", path=".")),
            asst(summary),
        ])
        agent = self.make_agent(llm=llm, max_rounds=3)
        events = list(agent.run("照方案推进任务"))

        self.assertEqual([k for k, _ in events],
                         ["round", "tool_call", "tool_result",
                          "round", "tool_call", "tool_result",
                          "round", "tool_call", "tool_result",
                          "round", "done"])
        rounds = [p for k, p in events if k == "round"]
        self.assertEqual(rounds[-1], {"round": 4, "wrap_up": True})  # 收尾轮标记
        done = events[-1][1]
        self.assertEqual(done["answer"], summary)
        self.assertEqual(done["stopped_reason"], "max_rounds")
        self.assertNotIn("stopped", done)  # 不是手动停止
        self.assertNotIn("强制停止", done["answer"])

        # 收尾轮请求：tools=None；末尾消息是合成指令（内容保留、_synthetic 已剥离）
        self.assertEqual(len(llm.requests), 4)
        wrap = llm.requests[3]
        self.assertIsNone(wrap["tools"])
        last = wrap["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("已达上限（3）", last["content"])
        self.assertIn("不要再调用任何工具", last["content"])
        self.assertFalse(any(k.startswith("_") for m in wrap["messages"] for k in m))

        # 历史：合成消息在（内存真相），assistant 总结带 _stats 收尾
        self.assertTrue(agent.history[-2].get("_synthetic"))
        self.assertEqual(agent.history[-1]["content"], summary)
        self.assertIn("_stats", agent.history[-1])

    def test_cancel_during_wrap_up_keeps_partial_answer(self):
        """收尾轮中途停止：半截文字 + （已手动停止）收尾，与主循环同一套行为。"""

        class CancelingLLM:
            def __init__(self):
                self.tools_seen = []

            def chat_stream(self, messages, tools=None, cancel=None):
                self.tools_seen.append(tools)
                if tools is None:  # 收尾轮：刚开流就被用户停止
                    cancel.set()
                    yield "delta", "总结到一半"
                    yield "message", {"role": "assistant", "content": "总结到一半"}
                else:
                    yield "message", tool_call_message(call("c1", "read_file", path="x"))

        llm = CancelingLLM()
        agent = self.make_agent(llm=llm, max_rounds=2)
        events = list(agent.run("问"))
        done = events[-1][1]
        self.assertTrue(done["stopped"])
        self.assertNotIn("stopped_reason", done)
        self.assertEqual(done["answer"], "总结到一半\n\n（已手动停止）")
        self.assertEqual(agent.history[-1]["_stats"]["stopped"], True)
        # 前两轮带工具清单，收尾轮 tools=None
        self.assertEqual(llm.tools_seen[-1], None)

    def test_wrap_up_error_propagates(self):
        """收尾轮请求炸了：异常原样上抛（worker 的 error + turn_end 收尾），
        历史停在「tool 结果 + 合成指令」，依然合法。"""
        llm = RecordingLLM([
            tool_call_message(call("c1", "read_file", path="x")),
            tool_call_message(call("c2", "read_file", path="x")),
            RuntimeError("服务商 500"),
        ])
        agent = self.make_agent(llm=llm, max_rounds=2)
        with self.assertRaises(RuntimeError):
            list(agent.run("问"))
        self.assertTrue(agent.history[-1].get("_synthetic"))  # 合成消息未落库也无妨
        self.assertFalse(any(m.get("role") == "assistant" and "_stats" in m
                             for m in agent.history))  # 没有半截总结被记入


# ---------------------------------------------------------------------------
# 二、合成消息生命周期
# ---------------------------------------------------------------------------

class TestSyntheticModelView(WrapupTestBase):

    def test_model_view_keeps_content_strips_marker(self):
        """_messages_for_model：合成消息以普通 user 身份发给模型（内容逐字保留），
        _synthetic 标记被 _clean_outgoing 剥掉——部分服务商会拒收未知字段。"""
        agent = self.make_agent()
        agent.history = [user("任务"), asst("进行中"), synthetic(WRAP_UP_INSTRUCTION.format(max_rounds=16))]
        view = agent._messages_for_model()
        self.assertEqual(view[-1]["role"], "user")
        self.assertIn("不要再调用任何工具", view[-1]["content"])
        self.assertFalse(any(k.startswith("_") for m in view for k in m))


class DbTestBase(unittest.TestCase):
    """临时库（test_storage.py 同款）：重定向 db.DB_PATH 后 init_db。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wrapup_test_db_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path


class TestSyntheticPersistence(DbTestBase):

    def test_save_messages_skips_synthetic_and_is_idempotent(self):
        """落库跳过 _synthetic：不写行、不进指纹账本；二次保存零写入；重启模拟
        （恢复 + 指纹重建）后重存仍零写入、恢复的历史里没有合成消息残留。"""
        saved = {}
        history = [
            user("任务"),
            tool_call_message(call("c1", "read_file", path="x")),
            tool_result("内容"),
            synthetic("本轮工具调用轮数已达上限（16）。不要再调用任何工具……"),
            asst("总结"),
        ]
        written = db.save_messages("s1", history, saved)
        self.assertEqual(written, 4)  # 合成消息不计入写入
        self.assertEqual(db.count_messages("s1"), 4)
        self.assertEqual(len(saved), 4)  # 指纹账本不收合成消息
        self.assertTrue(all(not m.get("_synthetic") for m in db.get_messages("s1")))

        # 幂等：同一份历史原样再存 → 0 写入
        self.assertEqual(db.save_messages("s1", history, saved), 0)

        # 重启模拟：窗口恢复（无合成消息）+ 指纹重建 + 重存 = 0 写入
        restored = db.restore_window("s1")
        self.assertEqual([m["content"] for m in restored],
                         ["任务", None, "内容", "总结"])  # tool_call 的 assistant 正文为 None
        saved2 = db.fingerprints(restored)
        self.assertEqual(db.save_messages("s1", restored, saved2), 0)
        # 内存里的合成消息仍在（真相在内存，DB 里没有）
        self.assertTrue(history[3].get("_synthetic"))


# ---------------------------------------------------------------------------
# 三、防失控提醒：循环指纹 + 轮数预算
# ---------------------------------------------------------------------------

class TestLoopReminders(WrapupTestBase):

    def synthetic_msgs(self, agent):
        # 只统计"系统提示"类提醒；收尾轮的合成指令也带 _synthetic，但不是提醒
        return [m for m in agent.history
                if m.get("_synthetic") and m["content"].startswith("（系统提示：")]

    def test_third_repeat_injects_once_and_fourth_does_not(self):
        """同参第 3 次触发提醒；同段第 4 次不再追加（== 阈值才触发）。
        max_rounds=10 → 预算轮 {0, 6}，剧本只走 5 轮，预算提醒不会掺进来。"""
        llm = RecordingLLM([tool_call_message(call(f"c{i}", "read_file", path="same.txt"))
                            for i in range(4)] + [asst("完成")])
        agent = self.make_agent(llm=llm, max_rounds=10)
        list(agent.run("问"))
        reminders = self.synthetic_msgs(agent)
        self.assertEqual(len(reminders), 1)
        self.assertIn("连续 3 次", reminders[0]["content"])
        self.assertIn("read_file", reminders[0]["content"])
        self.assertEqual(agent._reminders_used, 1)

    def test_streak_resets_on_different_args_and_can_retrigger(self):
        """换参数重置 streak；再次凑满 3 次可再次提醒（第二轮数避开预算提醒轮，
        max_rounds=25 → 预算轮 {15, 21}，剧本只到第 11 轮）。"""
        llm = RecordingLLM([
            tool_call_message(call("c1", "read_file", path="a")),  # r1
            tool_call_message(call("c2", "read_file", path="a")),  # r2 streak=2
            tool_call_message(call("c3", "read_file", path="b")),  # r3 重置
            *(tool_call_message(call(f"c{i}", "read_file", path="a")) for i in range(4, 7)),   # r4-6 → 提醒1
            tool_call_message(call("c7", "read_file", path="b")),  # r7 再重置
            *(tool_call_message(call(f"c{i}", "read_file", path="a")) for i in range(8, 11)),  # r8-10 → 提醒2
            asst("完成"),                                          # r11
        ])
        agent = self.make_agent(llm=llm, max_rounds=25)
        list(agent.run("问"))
        reminders = self.synthetic_msgs(agent)
        self.assertEqual(len(reminders), 2)
        self.assertEqual(agent._reminders_used, 2)

    def test_total_reminders_capped_by_budget(self):
        """5 个可触发的重复段 + 预算提醒机会，全部受 MAX_TURN_REMINDERS 封顶。"""
        script = []
        for i in range(15):  # 5 段 × 3 次重复（A/B 交替），每段都会"想"提醒
            path = "a" if (i // 3) % 2 == 0 else "b"
            script.append(tool_call_message(call(f"c{i}", "read_file", path=path)))
        script.append(asst("完成"))
        llm = RecordingLLM(script)
        agent = self.make_agent(llm=llm, max_rounds=40)
        list(agent.run("问"))
        self.assertEqual(len(self.synthetic_msgs(agent)), MAX_TURN_REMINDERS)
        self.assertEqual(agent._reminders_used, MAX_TURN_REMINDERS)

    def test_budget_reminder_fires_at_configured_rounds_only(self):
        """无重复调用时只有预算提醒：max_rounds-10 与 max_rounds-4 轮各一次。"""
        llm = RecordingLLM([tool_call_message(call(f"c{i}", "read_file", path=f"f{i}.txt"))
                            for i in range(16)] + [asst("兜底总结")])
        agent = self.make_agent(llm=llm, max_rounds=16)
        events = list(agent.run("问"))
        reminders = self.synthetic_msgs(agent)
        self.assertEqual(len(reminders), 2)
        self.assertIn("第 6 轮 / 上限 16 轮", reminders[0]["content"])
        self.assertIn("第 12 轮 / 上限 16 轮", reminders[1]["content"])
        # 预算提醒与收尾轮共存：最后一轮仍是收尾轮
        self.assertEqual(events[-1][1]["stopped_reason"], "max_rounds")

    def test_signature_ignores_key_order_and_whitespace(self):
        """指纹只认语义：键序/空白不同 → 同指纹；参数/工具不同 → 异指纹；
        解析失败退回原始文本且稳定；超长参数也只留指纹。"""
        a1 = Agent._tool_signature("write_file", '{"path": "a.md", "content": "x"}')
        a2 = Agent._tool_signature("write_file", '{ "content": "x", "path":"a.md" }')
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, Agent._tool_signature("write_file", '{"path": "b.md", "content": "x"}'))
        self.assertNotEqual(a1, Agent._tool_signature("read_file", '{"path": "a.md"}'))
        self.assertEqual(Agent._tool_signature("w", "不是json"),
                         Agent._tool_signature("w", "不是json"))
        self.assertEqual(Agent._tool_signature("w", None), Agent._tool_signature("w", None))
        Agent._tool_signature("write_file", json.dumps({"content": "x" * 50000}))  # 不炸即可


# ---------------------------------------------------------------------------
# 四、Anthropic 适配器：合成 user 消息并入 tool 结果所在的 user turn
# ---------------------------------------------------------------------------

class TestAnthropicSyntheticMerge(unittest.TestCase):

    def test_synthetic_user_merges_into_tool_result_turn(self):
        """Anthropic 协议要求 user turn 内 tool_result 在前、文本在后，且不接受
        连续两条 user 消息——tool 结果之后紧跟的合成提醒必须并进同一个 user turn。"""
        client = AnthropicMessagesClient(api_key="k", base_url="https://x/v1", model="m")
        messages = [
            user("任务"),
            tool_call_message(call("c1", "read_file", path="x")),
            tool_result("内容", "c1"),
            synthetic("（系统提示：你已连续 3 次以完全相同的参数调用工具 read_file。……）"),
        ]
        body = client._to_anthropic(messages, tools=None)
        convo = body["messages"]
        # 无连续同角色消息（Anthropic 硬性要求）
        self.assertTrue(all(convo[i]["role"] != convo[i + 1]["role"]
                            for i in range(len(convo) - 1)))
        # 最后一条 user turn：tool_result 块在前、提醒文本块在后
        last = convo[-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual([b["type"] for b in last["content"]], ["tool_result", "text"])
        self.assertIn("连续 3 次", last["content"][-1]["text"])
        # tools=None 的请求体不带 tools/tool_choice（收尾轮可能就是这种请求）
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
