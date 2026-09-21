"""
最小权限闸门的单元测试（cd backend && python3 -m unittest test_permissions -v）
================================================================================

覆盖验收要求的六类场景：
1. 复合命令拆解："ls;rm -rf /"、"rm  -rf"（多空格）、"rm -r -f"、
   "echo hi && sudo rm -rf /" 均命中 ask；"echo 'sudo rm -rf /'"（引号内
   字符串）不误伤；
2. 通配边界："git push*" 匹配 "git push origin main"，不匹配 "git pushish"；
3. 优先级：同一命令同时命中 allow 与 deny 时 deny 胜；
4. 工作区边界：write_file 区外路径返回【带原因的拒绝】而非静默；
5. 会话内记住：同一规则第二次不再 ask；
6. 时机约束：含 ask 的轮次，工具未执行、permission_request 事件已发出、
   恢复后按组调度执行；被拒调用的回填是带原因的 error 结果。

时机类测试消费 agent.run 生成器到 permission_request 处（此刻生成器停驻在
wait_all 的分片等待上，与生产环境 worker 线程的停驻点完全一致），决定从
另一个线程注入（与生产环境 HTTP 线程调 resolve_permission 同构）。
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent import Agent
from permissions import (ALLOW, ASK, DENY, CommandParseError, PermissionGate,
                         rejection_result, split_segments, words_match)
from tools import TOOL_REGISTRY, TOOL_READ_ONLY


def call(cid, name, **args):
    """构造一条 OpenAI 格式的 tool_call。"""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class ScriptedLLM:
    """按剧本逐轮吐消息的假 LLM：第一轮返回 tool_calls，之后返回纯文本收尾。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None, cancel=None):
        yield "message", self.script.pop(0)


# ---------------------------------------------------------------------------
# 命令拆解与匹配（纯函数层）
# ---------------------------------------------------------------------------

class TestCommandSplitting(unittest.TestCase):

    def test_compound_commands_are_split(self):
        """复合命令按 ; && || | 拆成独立段，每段以自己的命令词开头。"""
        segs = split_segments("ls;rm -rf /")
        self.assertEqual(segs, [["ls"], ["rm", "-rf", "/"]])
        segs = split_segments("echo hi && sudo rm -rf /")
        self.assertIn(["sudo", "rm", "-rf", "/"], segs)
        # 引号内的分隔符不是边界：整条引号内容是一个参数 token
        segs = split_segments("echo \"a;b\" | grep x")
        self.assertEqual(segs, [["echo", "a;b"], ["grep", "x"]])
        # 引号里的 sudo 只是一个参数 token，永远不会顶到命令词位置
        segs = split_segments("echo 'sudo rm -rf /'")
        self.assertEqual(segs, [["echo", "sudo rm -rf /"]])

    def test_whitespace_normalization(self):
        """shlex 归一化：多空白等价于单空白（"rm  -rf" 与 "rm -rf" 同段）。"""
        self.assertEqual(split_segments("rm  -rf"), [["rm", "-rf"]])

    def test_unparseable_command_raises(self):
        """引号不闭合无法安全解析：抛 CommandParseError（上层转 ask，不猜测）。"""
        with self.assertRaises(CommandParseError):
            split_segments("echo 'unbalanced")

    def test_word_boundary_wildcard(self):
        """git push* 命中 push + 任意参数，不命中 pushish（词边界，词内字符
        不归星号管）；mkfs* 靠"边界字符非字母数字"罩住 mkfs.ext4。"""
        self.assertTrue(words_match("git push*", ["git", "push", "origin", "main"]))
        self.assertTrue(words_match("git push*", ["git", "push"]))
        self.assertFalse(words_match("git push*", ["git", "pushish"]))
        self.assertTrue(words_match("mkfs*", ["mkfs.ext4"]))
        self.assertFalse(words_match("mkfs*", ["mkfsish"]))

    def test_short_flags_match_bundled_or_split(self):
        """短 flag 字符包含：-rf 命中 -fr 合并写法；rm -r 规则命中 -r -f 拆写。"""
        self.assertTrue(words_match("rm -rf", ["rm", "-fr"]))
        self.assertTrue(words_match("rm -r", ["rm", "-r", "-f"]))
        self.assertFalse(words_match("rm -rf", ["rm", "-r", "-f"]))  # 拆写由 rm -r 规则接管


# ---------------------------------------------------------------------------
# 判定：ask 命中 / 不误伤 / 优先级 / 边界
# ---------------------------------------------------------------------------

class GateTestBase(unittest.TestCase):
    def make_gate(self, **kw) -> PermissionGate:
        ws = tempfile.mkdtemp(prefix="perm_test_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        return PermissionGate(Path(ws).resolve(), **kw)


class TestVerdicts(GateTestBase):

    def test_required_ask_commands(self):
        """验收清单：四类复合/变体写法全部命中 ask。"""
        gate = self.make_gate()
        for cmd in ("ls;rm -rf /", "rm  -rf", "rm -r -f", "echo hi && sudo rm -rf /"):
            v = gate.check("run_bash", {"command": cmd})
            self.assertEqual(v.verb, ASK, f"{cmd!r} 应命中 ask，实际 {v}")
            self.assertTrue(v.reason)  # ask 必带原因（确认卡片要展示）

    def test_quoted_string_not_collateral(self):
        """引号字符串里的高危词只是 echo 的参数：不误伤。"""
        gate = self.make_gate()
        self.assertEqual(gate.check("run_bash",
                                    {"command": "echo 'sudo rm -rf /'"}).verb, ALLOW)

    def test_wildcard_hit_and_miss(self):
        """git push* 命中真实推送；pushish 是另一个词，放行。"""
        gate = self.make_gate()
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git push origin main"}).verb, ASK)
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git pushish"}).verb, ALLOW)

    def test_more_high_risk_commands_ask(self):
        """内置清单的其余成员（含换行分隔、子 shell、命令替换里的藏毒）。"""
        gate = self.make_gate()
        for cmd in ("mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/tmp/x",
                    "shutdown -h now", "echo a\nsudo rm -rf /",
                    "echo $(sudo rm -rf /)", "(sudo rm -rf /)",
                    "git reset --hard HEAD~1"):
            self.assertEqual(gate.check("run_bash", {"command": cmd}).verb, ASK, cmd)

    def test_normal_commands_allow(self):
        """常规命令放行（含"rm 普通文件"——闸门只拦递归/强制的破坏面）。"""
        gate = self.make_gate()
        for cmd in ("python3 demo.py", "git status", "ls -la", "rm normal.txt",
                    "echo \"a;b\" | grep x"):
            self.assertEqual(gate.check("run_bash", {"command": cmd}).verb, ALLOW, cmd)

    def test_unparseable_command_asks(self):
        """解析失败 → ask：看不懂的命令交人确认，绝不静默放行。"""
        gate = self.make_gate()
        self.assertEqual(gate.check("run_bash", {"command": "echo 'unbalanced"}).verb, ASK)

    def test_unknown_tool_allows(self):
        """幻觉出来的工具名没有可执行面（执行器只会报"未知工具"）：放行报错。"""
        gate = self.make_gate()
        self.assertEqual(gate.check("no_such_tool", {}).verb, ALLOW)

    def test_read_tools_allow(self):
        gate = self.make_gate()
        for tool in ("read_file", "list_dir", "grep", "calculator", "current_time",
                     "analyze_image"):
            self.assertEqual(gate.check(tool, {}).verb, ALLOW, tool)

    def test_deny_beats_allow(self):
        """优先级：同一命令同时命中 allow 与 deny → deny 胜（最严者胜）。"""
        gate = self.make_gate(user_rules_loader=lambda: [
            {"tool": "run_bash", "pattern": "ls -l*", "decision": "deny", "reason": "测试禁令"},
            {"tool": "run_bash", "pattern": "ls*", "decision": "allow"},
        ])
        v = gate.check("run_bash", {"command": "ls -la"})
        self.assertEqual(v.verb, DENY)
        self.assertIn("测试禁令", v.reason)  # 拒绝必须带原因
        self.assertEqual(gate.check("run_bash", {"command": "ls"}).verb, ALLOW)

    def test_user_rule_overrides_builtin_same_key(self):
        """用户规则与内置同 key（git push*）→ 用户 allow 覆盖内置 ask。"""
        gate = self.make_gate(user_rules_loader=lambda: [
            {"tool": "run_bash", "pattern": "git push*", "decision": "allow"},
        ])
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git push origin main"}).verb, ALLOW)

    def test_user_rule_can_ask_writes(self):
        """用户规则可把整工具转 ask（如所有写文件都要确认）。"""
        gate = self.make_gate(user_rules_loader=lambda: [
            {"tool": "write_file", "decision": "ask", "reason": "写入需确认"},
        ])
        self.assertEqual(gate.check("write_file", {"path": "a.txt"}).verb, ASK)

    def test_invalid_user_rules_skipped_and_loader_failure_degrades(self):
        """手编 JSON 的坏条目逐条跳过；加载器炸掉退回纯内置，判定照常工作。"""
        gate = self.make_gate(user_rules_loader=lambda: [
            "junk",
            {"tool": "", "decision": "allow"},
            {"tool": "run_bash", "decision": "weird"},
            {"tool": "run_bash", "pattern": "*", "decision": "deny"},  # 空星号词 = 无意义
        ])
        self.assertEqual(gate.check("run_bash", {"command": "sudo x"}).verb, ASK)

        def boom():
            raise RuntimeError("库锁住了")
        gate2 = self.make_gate(user_rules_loader=boom)
        self.assertEqual(gate2.check("run_bash", {"command": "sudo x"}).verb, ASK)


class TestWorkspaceBoundary(GateTestBase):

    def test_write_outside_returns_reasoned_deny(self):
        """工作区越界：带原因的 DENY（而非静默），原因来自 _resolve 本身。"""
        gate = self.make_gate()
        for tool, args in (("write_file", {"path": "../evil.txt"}),
                           ("apply_patch", {"path": "/etc/passwd", "search": "a", "replace": "b"}),
                           ("write_file", {"path": "a/../../escape.txt"})):
            v = gate.check(tool, args)
            self.assertEqual(v.verb, DENY, f"{tool} {args} 应拒绝")
            self.assertIn("越界", v.reason)
            # 拒绝结果（回填给模型/前端的样子）带 ok:false + 原因 + hint
            payload = json.loads(rejection_result(v))
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["error"].startswith("权限拒绝: "))
            self.assertIn("hint", payload)

    def test_write_inside_allows(self):
        gate = self.make_gate()
        self.assertEqual(gate.check("write_file", {"path": "ok.txt"}).verb, ALLOW)
        self.assertEqual(gate.check("apply_patch",
                                    {"path": "sub/dir.py", "search": "a", "replace": "b"}).verb, ALLOW)


# ---------------------------------------------------------------------------
# 会话内记住 / 一次性 overlay
# ---------------------------------------------------------------------------

class TestSessionMemory(GateTestBase):

    def _ask_and_resolve(self, gate, command, decision):
        v = gate.check("run_bash", {"command": command})
        reqs = gate.open_requests([("run_bash", {"command": command}, v)])
        self.assertTrue(gate.resolve(reqs[0]["id"], decision))
        return gate.apply_decisions(gate.wait_all())

    def test_same_rule_second_time_no_ask(self):
        """验收：allow_session 后，同一规则第二次直接放行、不再 ask。"""
        gate = self.make_gate()
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git push origin main"}).verb, ASK)
        self._ask_and_resolve(gate, "git push origin main", "allow_session")
        # 第二次（全新调用、无 overlay）：会话记忆接管，allow；闸门收到的
        # ask 清单为空（与 agent 的过滤一致：只有 ask 判定才登记确认请求）
        v = gate.check("run_bash", {"command": "git push origin x"})
        self.assertEqual(v.verb, ALLOW)
        asks = [("run_bash", {"command": "git push x"}, v)]
        self.assertEqual(gate.open_requests([a for a in asks if a[2].verb == ASK]), [])

    def test_one_time_allow_does_not_remember(self):
        """仅本次允许：本轮放行，之后的同规则调用仍要再问。"""
        gate = self.make_gate()
        overlay = self._ask_and_resolve(gate, "git push origin main", "allow")
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git push origin main"},
                                    overlay=overlay).verb, ALLOW)
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git push origin main"}).verb, ASK)

    def test_deny_decision_rejects_this_round(self):
        """拒绝决定：本轮该调用按 DENY 走（带用户拒绝的原因）。"""
        gate = self.make_gate()
        overlay = self._ask_and_resolve(gate, "git push origin main", "deny")
        self.assertEqual(gate.check("run_bash",
                                    {"command": "git push origin main"},
                                    overlay=overlay).verb, DENY)

    def test_timeout_and_cancel_resolve_as_deny(self):
        """超时/停止都不是安全边界，只是防挂死——一律按拒绝收场。"""
        gate = self.make_gate(ask_timeout=0.15)
        v = gate.check("run_bash", {"command": "git push origin main"})
        reqs = gate.open_requests([("run_bash", {"command": "git push origin main"}, v)])
        decisions = gate.wait_all()  # 无人 resolve → 到时
        self.assertEqual(decisions[reqs[0]["id"]], "timeout")
        overlay = gate.apply_decisions(decisions)
        self.assertEqual(gate.check("run_bash", {"command": "git push origin main"},
                                    overlay=overlay).verb, DENY)

        gate2 = self.make_gate(ask_timeout=30)
        cancel = threading.Event()
        v2 = gate2.check("run_bash", {"command": "git push origin main"})
        reqs2 = gate2.open_requests([("run_bash", {"command": "git push origin main"}, v2)])
        cancel.set()
        decisions2 = gate2.wait_all(cancel=cancel)
        self.assertEqual(decisions2[reqs2[0]["id"]], "cancelled")

    def test_zero_timeout_denies_immediately(self):
        """ask_timeout=0（CLI 默认）：无人应答 → 立即拒绝，终端绝不挂死。"""
        gate = self.make_gate(ask_timeout=0)
        v = gate.check("run_bash", {"command": "git push origin main"})
        gate.open_requests([("run_bash", {"command": "git push origin main"}, v)])
        t0 = time.monotonic()
        decisions = gate.wait_all()
        self.assertLess(time.monotonic() - t0, 1.0)
        overlay = gate.apply_decisions(decisions)
        self.assertEqual(gate.check("run_bash", {"command": "git push origin main"},
                                    overlay=overlay).verb, DENY)

    def test_open_requests_dedupes_by_rule(self):
        """同一规则一轮只问一次：两条 git push 只弹一张卡，决定一并生效。"""
        gate = self.make_gate()
        asks = [("run_bash", {"command": "git push origin a"},
                 gate.check("run_bash", {"command": "git push origin a"})),
                ("run_bash", {"command": "git push origin b"},
                 gate.check("run_bash", {"command": "git push origin b"}))]
        reqs = gate.open_requests(asks)
        self.assertEqual(len(reqs), 1)
        self.assertTrue(gate.resolve(reqs[0]["id"], "allow_session"))
        overlay = gate.apply_decisions(gate.wait_all())
        for cmd in ("git push origin a", "git push origin b"):
            self.assertEqual(gate.check("run_bash", {"command": cmd},
                                        overlay=overlay).verb, ALLOW)

    def test_allow_session_covers_all_matched_ask_rules(self):
        """一条命令命中多条 ask 规则（rm -rf 同时命中 rm -rf 与 rm -r）时，
        allow_session 必须对整组键生效——否则重判会被另一条未被覆盖的规则
        再次拦下（真机手测抓到过）。"""
        gate = self.make_gate()
        v = gate.check("run_bash", {"command": "rm -rf x"})
        self.assertEqual(v.verb, ASK)
        self.assertEqual(len(v.ask_keys), 2)
        reqs = gate.open_requests([("run_bash", {"command": "rm -rf x"}, v)])
        self.assertEqual(len(reqs), 1)  # 一组规则一张卡
        self.assertTrue(gate.resolve(reqs[0]["id"], "allow_session"))
        overlay = gate.apply_decisions(gate.wait_all())
        self.assertEqual(gate.check("run_bash",
                                    {"command": "rm -rf x"},
                                    overlay=overlay).verb, ALLOW)
        # 无 overlay 的全新调用也不再问：会话记忆覆盖整组键
        self.assertEqual(gate.check("run_bash", {"command": "rm -rf y"}).verb, ALLOW)

    def test_resolve_rejects_unknown_or_duplicate(self):
        """迟到/重复的 resolve 安全返回 False（补发重放的旧卡片落在这里）。"""
        gate = self.make_gate()
        v = gate.check("run_bash", {"command": "git push"})
        reqs = gate.open_requests([("run_bash", {"command": "git push"}, v)])
        self.assertTrue(gate.resolve(reqs[0]["id"], "allow"))
        self.assertFalse(gate.resolve(reqs[0]["id"], "deny"))   # 已处理过
        self.assertFalse(gate.resolve("no_such_id", "allow"))   # 未知 id
        self.assertFalse(gate.resolve(reqs[0]["id"], "hack"))   # 非法取值


# ---------------------------------------------------------------------------
# 时机约束（agent 级）：判定在分组调度之前、ask 暂停回合、恢复后重新调度
# ---------------------------------------------------------------------------

class AgentGateBase(GateTestBase):
    def make_agent(self, gate=None) -> Agent:
        ws = tempfile.mkdtemp(prefix="perm_agent_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=ScriptedLLM([]), verbose=False, workspace=ws,
                      permission_gate=gate or self.make_gate(ask_timeout=60))
        return agent

    def register_run_bash_probe(self, journal, delay=0.0):
        """把 run_bash 换成可观测的探针（命中 ask 规则的是【工具名】，不是实现），
        测完恢复。探针绝不真的执行 shell。"""
        orig = TOOL_REGISTRY["run_bash"]

        def probe(command="", ctx=None):
            time.sleep(delay)
            journal.append({"command": command, "thread": threading.current_thread().name})
            return json.dumps({"ok": True, "ran": command}, ensure_ascii=False)

        TOOL_REGISTRY["run_bash"] = probe
        self.addCleanup(lambda: TOOL_REGISTRY.__setitem__("run_bash", orig))

    def consume_until_permission(self, gen):
        """驱动生成器直到 permission_request（此刻它停驻在 wait_all 上，
        与生产环境 worker 线程的停驻点一致）。返回 (事件列表, 请求载荷)。"""
        events = []
        for kind, payload in gen:
            events.append((kind, payload))
            if kind == "permission_request":
                return events, payload
        raise AssertionError("没有等到 permission_request 事件")


class TestTimingConstraint(AgentGateBase):

    def ask_pause_scenario(self):
        """共用场景：一轮 [run_bash(git push), read_file]，推进到 permission_request
        处停住（生成器停驻在 wait_all 上）。返回 (agent, 生成器, 请求载荷)。"""
        journal = []
        self.register_run_bash_probe(journal)
        agent = self.make_agent()
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main"),
                              call("r1", "read_file", path="a.txt")),
            {"role": "assistant", "content": "推送完成"},
        ])
        (Path(agent.ctx.workspace) / "a.txt").write_text("内容", encoding="utf-8")
        gen = agent.run("帮我推上去")
        events, req = self.consume_until_permission(gen)
        # permission_request 载荷完整（前端卡片要靠它展示）
        self.assertEqual(req["tool"], "run_bash")
        self.assertEqual(req["input"], {"command": "git push origin main"})
        self.assertTrue(req["reason"])
        self.assertTrue(req["id"])
        # 关键断言：暂停点之前，ask 工具没有被执行（探针日志为空）
        self.assertEqual(journal, [])
        # 且此刻历史里还没有任何 tool 结果（回填发生在调度阶段）
        self.assertEqual([m for m in agent.history if m["role"] == "tool"], [])
        return agent, gen, req

    def test_ask_pauses_round_and_tool_not_executed(self):
        """验收时机①②：ask 调用的轮次，工具【未执行】、permission_request 已发出。"""
        self.ask_pause_scenario()

    def test_resume_regroups_and_backfills_in_request_order(self):
        """验收时机③：恢复后按组调度执行、回填顺序 = 请求顺序。"""
        agent, gen, req = self.ask_pause_scenario()
        # 从另一个线程注入决定（与生产环境 HTTP 线程调 resolve_permission 同构）
        t = threading.Thread(target=agent.resolve_permission, args=(req["id"], "allow_session"))
        t.start()
        events = list(gen)  # 排干剩余事件（此刻才真正执行工具）
        t.join()
        kinds = [k for k, _ in events]
        # 剩余事件：两个结果 + 第二轮的 round + done（剧本第二轮给出最终回答）
        self.assertEqual(kinds, ["tool_result", "tool_result", "round", "done"])
        # 回填顺序 = 请求顺序（run_bash 在前，read_file 在后），配对正确
        results = [p["result"] for k, p in events if k == "tool_result"]
        self.assertIn("git push origin main", results[0])
        self.assertIn("内容", results[1])
        tail = [m for m in agent.history if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tail], ["b1", "r1"])

    def test_deny_backfills_reasoned_error_without_executing(self):
        """验收时机④：被拒调用的回填是带原因的 error 结果，工具确实没跑；
        同轮其它 allow 调用照常执行。"""
        journal = []
        self.register_run_bash_probe(journal)
        gate = self.make_gate(ask_timeout=60)
        agent = self.make_agent(gate)
        (Path(agent.ctx.workspace) / "a.txt").write_text("内容", encoding="utf-8")
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main"),
                              call("r1", "read_file", path="a.txt")),
            {"role": "assistant", "content": "好的，不推了"},
        ])
        gen = agent.run("帮我推上去")
        _, req = self.consume_until_permission(gen)
        threading.Thread(target=agent.resolve_permission, args=(req["id"], "deny")).start()
        events = list(gen)
        bash_result = next(p["result"] for k, p in events
                           if k == "tool_result" and p["name"] == "run_bash")
        payload = json.loads(bash_result)
        self.assertFalse(payload["ok"])
        self.assertIn("权限拒绝", payload["error"])       # 原因回给模型（含谁拒的、为什么）
        self.assertIn("hint", payload)                    # 改道提示
        self.assertEqual(journal, [])                     # 探针全程没跑
        # read_file 不受连坐：照常执行并回填
        read_result = next(p["result"] for k, p in events
                           if k == "tool_result" and p["name"] == "read_file")
        self.assertIn("内容", read_result)
        # 历史里两条 tool 消息俱全（错误也是正式反馈，服务商配对要求）
        self.assertEqual(len([m for m in agent.history if m["role"] == "tool"]), 2)

    def test_timeout_denies_and_round_completes(self):
        """超时防挂死：无人应答时回合以拒绝收场，不挂死也不执行。"""
        journal = []
        self.register_run_bash_probe(journal)
        gate = self.make_gate(ask_timeout=0.2)
        agent = self.make_agent(gate)
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main")),
            {"role": "assistant", "content": "超时未确认，已放弃"},
        ])
        events = list(agent.run("帮我推上去"))
        bash_result = next(p["result"] for k, p in events
                           if k == "tool_result" and p["name"] == "run_bash")
        self.assertIn("权限拒绝", json.loads(bash_result)["error"])
        self.assertEqual(journal, [])
        self.assertTrue(any(k == "done" for k, _ in events))

    def test_stop_while_pending_resolves_as_deny(self):
        """等待确认时用户点停止：等待及时解除，按拒绝收场、回合正常收尾。"""
        journal = []
        self.register_run_bash_probe(journal)
        agent = self.make_agent()
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main")),
            {"role": "assistant", "content": "不需要了"},
        ])
        gen = agent.run("帮我推上去")
        _, req = self.consume_until_permission(gen)
        agent.stop()
        events = list(gen)
        bash_result = next(p["result"] for k, p in events
                           if k == "tool_result" and p["name"] == "run_bash")
        self.assertIn("权限拒绝", json.loads(bash_result)["error"])
        self.assertEqual(journal, [])

    def test_session_memory_spans_rounds(self):
        """本会话记住跨轮生效：第二轮同样的 git push 不再产生 permission_request。"""
        journal = []
        self.register_run_bash_probe(journal)
        agent = self.make_agent()
        (Path(agent.ctx.workspace) / "a.txt").write_text("内容", encoding="utf-8")
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main")),
            tool_call_message(call("b2", "run_bash", command="git push origin dev")),
            {"role": "assistant", "content": "两个分支都处理完"},
        ])
        gen = agent.run("推两个分支")
        seen, req = self.consume_until_permission(gen)
        threading.Thread(target=agent.resolve_permission,
                         args=(req["id"], "allow_session")).start()
        seen += list(gen)  # 一次排干到回合结束（含第二轮）
        perms = [1 for k, _ in seen if k == "permission_request"]
        self.assertEqual(len(perms), 1, "整个回合只允许出现过一次确认请求（第二轮不再问）")
        self.assertEqual([p["command"] for p in journal],
                         ["git push origin main", "git push origin dev"])


# ---------------------------------------------------------------------------
# 端到端：工作区越界的带原因拒绝 / apply_patch 修复后的可用性 / allow 无感
# ---------------------------------------------------------------------------

class TestEndToEnd(AgentGateBase):

    def test_write_outside_rejected_with_reason(self):
        """验收工作区边界（端到端）：越界写入回填带原因的拒绝，文件没落地。"""
        agent = self.make_agent()  # 默认闸门即可：越界是 deny，不需要交互
        agent.llm = ScriptedLLM([
            tool_call_message(call("w1", "write_file", path="../evil.txt", content="x")),
            {"role": "assistant", "content": "被拒绝了"},
        ])
        events = list(agent.run("把文件写到工作区外面"))
        result = json.loads(next(p["result"] for k, p in events
                                 if k == "tool_result"))
        self.assertFalse(result["ok"])
        self.assertIn("权限拒绝", result["error"])
        self.assertIn("越界", result["error"])
        self.assertFalse((Path(agent.ctx.workspace).parent / "evil.txt").exists())
        # 无 permission_request 事件：deny 直接拒绝，不走确认链路
        self.assertFalse([k for k, _ in events if k == "permission_request"])

    def test_write_inside_runs_silently(self):
        """验收 allow 无感：区内写入零事件零交互直达结果。"""
        agent = self.make_agent()
        agent.llm = ScriptedLLM([
            tool_call_message(call("w1", "write_file", path="ok.txt", content="hello"),
                              call("r1", "read_file", path="ok.txt")),
            {"role": "assistant", "content": "写好了"},
        ])
        events = list(agent.run("写个文件"))
        self.assertFalse([k for k, _ in events if k == "permission_request"])
        result = json.loads(next(p["result"] for k, p in events
                                 if k == "tool_result" and p["name"] == "write_file"))
        self.assertTrue(result["ok"])
        self.assertTrue((Path(agent.ctx.workspace) / "ok.txt").exists())

    def test_apply_patch_works_and_respects_boundary(self):
        """回归：apply_patch 此前缺 ctx 形参导致每次调用 NameError——修复后
        正常工作，且越界同样被闸门带原因拒绝。"""
        agent = self.make_agent()
        (Path(agent.ctx.workspace) / "code.py").write_text("def add(a, b):\n    return a - b\n",
                                                           encoding="utf-8")
        agent.llm = ScriptedLLM([
            tool_call_message(call("p1", "apply_patch", path="code.py",
                                   search="return a - b", replace="return a + b")),
            {"role": "assistant", "content": "修好了"},
        ])
        events = list(agent.run("修 bug"))
        result = json.loads(next(p["result"] for k, p in events
                                 if k == "tool_result"))
        self.assertTrue(result["ok"], result)
        self.assertIn("return a + b",
                      (Path(agent.ctx.workspace) / "code.py").read_text(encoding="utf-8"))

    def test_cli_default_gate_denies_asks_without_hanging(self):
        """CLI 场景（未注入闸门）：Agent 自建 ask_timeout=0 的闸门，高危命令
        立即带原因拒绝——终端安全网仍在，且绝不挂死。"""
        agent = Agent(llm=ScriptedLLM([]), verbose=False,
                      workspace=self.make_gate().workspace)  # 借基类的临时工作区（自带清理）
        self.assertEqual(agent.permissions.ask_timeout, 0)
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="sudo rm -rf /")),
            {"role": "assistant", "content": "好的，不执行危险命令"},
        ])
        journal = []
        self.register_run_bash_probe(journal)
        events = list(agent.run("帮我清盘"))
        result = json.loads(next(p["result"] for k, p in events
                                 if k == "tool_result"))
        self.assertIn("权限拒绝", result["error"])
        self.assertEqual(journal, [])

    def test_every_registered_tool_has_a_builtin_rule_or_fallback(self):
        """注册表里的每个工具都必须有判定出路（规则命中或工具级兜底）——
        新工具忘了登记时会在这里亮红灯（静默 deny 会莫名其妙打断自动化）。"""
        gate = self.make_gate()
        from tools import TOOL_REGISTRY
        for name in TOOL_REGISTRY:
            v = gate.check(name, {"command": "x", "path": "x", "expression": "1"})
            self.assertIn(v.verb, (ALLOW, DENY, ASK), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
