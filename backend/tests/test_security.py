"""
最小安全测试集（cd backend && python3 -m unittest tests.test_security -v）
==========================================================================

只覆盖「错了会出安全事故或数据损坏」的部分，不追求覆盖率。每个测试类对应
一类事故，docstring 写明它防的是什么：

1. TestPathBoundary      —— 路径越界导致任意文件读/写（三道独立的闸：
                            code_tools._resolve / db.read_artifact /
                            memory 文件名白名单 + realpath，任何一道被绕过
                            都是安全事故）；
2. TestApplyPatchSafety  —— apply_patch 锚定错改：多处命中改错位置、
                            正则元字符崩溃、replace==search 无谓重写；
3. TestCommandParsingE2E —— 权限闸门的命令解析被复合/拆写/换行写法绕过，
                            或反向误伤引号内的普通字符串；
4. TestConcurrentSave    —— 并发落盘产生重复行/ord 断裂/指纹账本互踩；
                            会话删除后的迟到写入留孤儿行；
5. TestCompactSafety     —— 压缩把边界插进悬空 tool 配对中间（下一轮请求
                            被服务商 400）、连续压缩双摘要/丢锚点；
6. TestMemorySafety      —— 记忆落盘路径逃逸、frontmatter 残缺入库、
                            超长正文、半套记忆（执行器必须全有或全无）；
7. TestPermissionTiming  —— ask 等待期间工具被偷跑 / 事件载荷残缺 /
                            恢复后回填错位 / 超时与停止没按拒绝收场
                            （与 test_permissions 互补：走 Agent + db 持久化
                            全链路）。

全部 mock LLM，不依赖网络与真实服务。
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import db
from agent import Agent
from code_tools import _resolve, apply_patch
from memory import apply_extraction, valid_memory_filename
from permissions import PermissionGate


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def user(text):
    return {"role": "user", "content": text}


def asst(text):
    return {"role": "assistant", "content": text}


def tool_call_asst(call_id, name, **args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args, ensure_ascii=False)}}]}


def tool_result(text, call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def msg(text):
    return {"role": "assistant", "content": text}


class ScriptedLLM:
    """按剧本逐轮吐消息的假 LLM（与 test_permissions 同构）。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None, cancel=None):
        yield "message", self.script.pop(0)


def make_ctx(ws: Path):
    """code_tools 的工具函数只需要 ctx.workspace 一个属性。"""
    return SimpleNamespace(workspace=str(ws))


def call(cid, name, **args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


# ---------------------------------------------------------------------------
# 一、路径边界：三道独立的闸
# ---------------------------------------------------------------------------

class TestPathBoundary(unittest.TestCase):
    """防的事故：路径越界 → 工作区外任意文件的读/写/删（含 .env 里的 API Key、
    用户主目录、系统文件）。三处校验是三道【独立】的闸——read_artifact 面向
    前端查询参数，memory 面向提取模型的产出，_resolve 面向模型工具调用；
    任何一道被绕过都构成安全事故，因此逐一回归，不允许"有一道把关就行"。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_path_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = (self.tmp / "ws").resolve()
        self.ws.mkdir()
        self.secret = self.tmp / "secret.txt"
        self.secret.write_text("机密", encoding="utf-8")

    # ---- 闸 1：code_tools._resolve（模型工具调用的读写边界）----

    def test_resolve_blocks_parent_escape(self):
        for evil in ("../secret.txt", "a/../../secret.txt", "sub/../../../secret.txt",
                     "..", "../"):
            with self.assertRaises(ValueError, msg=f"应拒绝 {evil!r}"):
                _resolve(evil, self.ws)

    def test_resolve_blocks_absolute_path(self):
        """绝对路径在 pathlib 拼接时会整个替换掉基准目录，必须拦下。"""
        with self.assertRaises(ValueError):
            _resolve(str(self.secret), self.ws)
        with self.assertRaises(ValueError):
            _resolve("/etc/passwd", self.ws)

    def test_resolve_blocks_symlink_escape(self):
        """符号链接逃逸：resolve() 消解软链之后必须重新校验——只做字符串
        前缀比较的实现对这道题会放行。"""
        (self.ws / "link").symlink_to(self.tmp)          # 链到工作区外的目录
        (self.ws / "file_link").symlink_to(self.secret)  # 链到工作区外的文件
        with self.assertRaises(ValueError):
            _resolve("link/secret.txt", self.ws)
        with self.assertRaises(ValueError):
            _resolve("file_link", self.ws)

    def test_resolve_allows_legitimate_paths(self):
        """放行面回归：区内的相对路径、子目录、区内软链都必须照常可用——
        安全测试不能只测"全拒"，否则把正常功能锁死也算事故。"""
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "a.txt").write_text("x", encoding="utf-8")
        (self.ws / "real.txt").write_text("y", encoding="utf-8")
        (self.ws / "inner").symlink_to(self.ws / "real.txt")
        self.assertEqual(_resolve("sub/a.txt", self.ws),
                         (self.ws / "sub" / "a.txt").resolve())
        self.assertEqual(_resolve("inner", self.ws),
                         (self.ws / "real.txt").resolve())
        self.assertEqual(_resolve(".", self.ws), self.ws)

    # ---- 闸 2：db.read_artifact（前端查询参数 → 归档文件）----

    def test_read_artifact_blocks_escapes(self):
        db.DB_PATH = self.tmp / "test.db"
        self.addCleanup(setattr, db, "DB_PATH",
                        Path(__file__).resolve().parent.parent / "data" / "agent_data.db")
        base = db.DB_PATH.parent / "artifacts" / "s1"
        base.mkdir(parents=True)
        (base / "real.json").write_text('{"role": "user", "content": "x"}', encoding="utf-8")
        (base / "evil.txt").write_text("x", encoding="utf-8")

        self.assertEqual(db.read_artifact("s1/real.json")["content"], "x")
        for evil in ("../secret.txt", "../../etc/passwd", "/etc/passwd",
                     str(self.secret), "s1/evil.txt", "s1/../../secret.json",
                     "", "s1", "."):
            with self.assertRaises(ValueError, msg=f"应拒绝 {evil!r}"):
                db.read_artifact(evil)

    # ---- 闸 3：memory（提取模型的产出 → 记忆目录）----

    def test_memory_filename_whitelist(self):
        for evil in ("../evil.md", "..", "/etc/passwd.md", "a/b.md", "大写.md",
                     "Not-Kebab.md", "with space.md", "a.md.exe", ".md", ".hidden.md",
                     "", None, 123):
            self.assertFalse(valid_memory_filename(evil), f"应拒绝 {evil!r}")
        for good in ("a.md", "user-pref.md", "9lives.md", "a-b-c.md"):
            self.assertTrue(valid_memory_filename(good), f"应放行 {good!r}")

    def test_memory_apply_extraction_blocks_escapes(self):
        """执行器层面（文件名白名单 + realpath 双闸）：../ 与绝对路径、
        以及"合法文件名但 realpath 逃出目录"的软链都不得落盘。"""
        mem_dir = self.tmp / ".agent-memory"
        mem_dir.mkdir()
        evil_ops = [{"action": "write", "file": "../evil.md",
                     "frontmatter": {"name": "evil", "description": "d",
                                     "metadata": {"type": "user"}},
                     "body": "b"},
                    {"action": "write", "file": str(self.secret),
                     "frontmatter": {"name": "x", "description": "d",
                                     "metadata": {"type": "user"}},
                     "body": "b"},
                    {"action": "delete", "file": "../../secret.txt"}]
        for op in evil_ops:
            out = apply_extraction(mem_dir, {"memories": [op]})
            self.assertTrue(out["abandoned"], f"应拒绝 {op['file']!r}")
            self.assertFalse((mem_dir / "evil.md").exists())
            self.assertEqual(self.secret.read_text(encoding="utf-8"), "机密")
            self.assertFalse((mem_dir / "MEMORY.md").exists())  # 索引也不得被创建


# ---------------------------------------------------------------------------
# 二、apply_patch 锚定安全
# ---------------------------------------------------------------------------

class TestApplyPatchSafety(unittest.TestCase):
    """防的事故：锚定式编辑改错地方——search 在文件中出现多次时若"改第一处"
    或"全部替换"，写坏的是用户代码（数据损坏）；search 里的正则元字符若被
    当正则解释会崩溃或错配。原则：宁失败、不错改。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_patch_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx = make_ctx(self.tmp)

    def _patch(self, content, search, replace):
        (self.tmp / "f.py").write_text(content, encoding="utf-8")
        return apply_patch("f.py", search, replace, ctx=self.ctx)

    def test_unique_hit_replaces(self):
        out = json.loads(self._patch("alpha\nbeta\n", "beta", "BETA"))
        self.assertTrue(out["ok"])
        self.assertEqual((self.tmp / "f.py").read_text(encoding="utf-8"), "alpha\nBETA\n")

    def test_multiple_hits_must_fail_not_guess(self):
        """多处命中必须失败：改"第一处"会静默写坏另一处语义相同的代码。"""
        out = json.loads(self._patch("foo\nfoo\nbar\n", "foo", "baz"))
        self.assertFalse(out["ok"])
        self.assertIn("2 处", out["error"])
        self.assertEqual((self.tmp / "f.py").read_text(encoding="utf-8"),
                         "foo\nfoo\nbar\n", "失败时文件必须原封不动")

    def test_no_hit_fails_with_hint(self):
        out = json.loads(self._patch("aaa\n", "bbb", "ccc"))
        self.assertFalse(out["ok"])
        self.assertIn("read_file", out.get("hint", ""))

    def test_regex_metachars_treated_literally(self):
        """search 是字面文本不是正则：.*、()、[]、中文括号都不崩、不错配。
        （apply_patch 的锚定语义是行首命中的整段原文，search 按行首对齐构造。）"""
        content = "value = a.*b()\nkeep = [x]\nother = （中文）\n"
        out = json.loads(self._patch(content, "value = a.*b()", "value = a+b()"))
        self.assertTrue(out["ok"], out)
        body = (self.tmp / "f.py").read_text(encoding="utf-8")
        self.assertIn("value = a+b()", body)
        self.assertIn("keep = [x]", body)
        out2 = json.loads(self._patch("axxxb\nreset\n", "other = （中文）", "other = （英文）"))
        self.assertFalse(out2["ok"], "换文件后旧锚点必须未命中而非错改")
        out3 = json.loads(self._patch("value = a.*b()\nother = （中文）\n",
                                      "other = （中文）", "other = （英文）"))
        self.assertTrue(out3["ok"], out3)
        self.assertIn("other = （英文）", (self.tmp / "f.py").read_text(encoding="utf-8"))
        # 字面 "a.*b" 不得匹配 "axxxb"（正则语义下会错配）
        out4 = json.loads(self._patch("axxxb\n", "a.*b", "REPLACED"))
        self.assertFalse(out4["ok"])
        self.assertEqual((self.tmp / "f.py").read_text(encoding="utf-8"), "axxxb\n")

    def test_replace_equal_to_search_changes_nothing(self):
        """replace == search（模型重发同一段）：文件内容不得变化。
        现状记录：当前实现仍会 write_text 一次，mtime 会前进，但内容逐字节
        不变（本用例锁的就是内容不变这条硬底线；mtime 无谓变化记录为已知
        现状，不影响正确性，不强求实现优化）。"""
        content = "def f():\n    return 1\n"
        out = json.loads(self._patch(content, "    return 1\n", "    return 1\n"))
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.tmp / "f.py").read_text(encoding="utf-8"), content)

    def test_line_number_column_in_search_still_anchors(self):
        """行号栏污染（模型把 read_file 的 "  17\\t" 原样抄进 search）不得
        让锚定把行号写进文件——行号化改造引入的新失败模式。"""
        out = json.loads(self._patch("def f():\n    return 1\n", "  2\t    return 1",
                                     "  2\t    return 2"))
        self.assertTrue(out["ok"], out)
        body = (self.tmp / "f.py").read_text(encoding="utf-8")
        self.assertEqual(body, "def f():\n    return 2\n")
        self.assertNotIn("\t", body)


# ---------------------------------------------------------------------------
# 三、命令解析端到端（mock LLM 完整回合）
# ---------------------------------------------------------------------------

class TestCommandParsingE2E(unittest.TestCase):
    """防的事故（两个方向）：拆解不彻底让 "ls;rm -rf /" 这类复合/拆写/换行
    命令绕过闸门直接执行（安全事故）；或反向用全文子串匹配误伤
    "echo 'sudo rm -rf /'" 这种引号参数（可用性事故）。在【完整回合】里验证：
    判定 → 事件 → 回填结构一条链都对。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_cmd_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run_round(self, command, ask_timeout=0.3):
        """跑一个真实回合：run_bash 换成探针（绝不真执行 shell），返回
        (事件列表, 探针日志, agent)。ask 无人应答时按超时拒绝收场。"""
        journal = []
        orig = __import__("tools").TOOL_REGISTRY["run_bash"]

        def probe(command="", ctx=None):
            journal.append(command)
            return json.dumps({"ok": True, "ran": command}, ensure_ascii=False)

        __import__("tools").TOOL_REGISTRY["run_bash"] = probe
        self.addCleanup(lambda: __import__("tools").TOOL_REGISTRY.__setitem__("run_bash", orig))

        gate = PermissionGate(self.tmp.resolve(), ask_timeout=ask_timeout)
        agent = Agent(llm=ScriptedLLM([]), verbose=False, workspace=str(self.tmp),
                      permission_gate=gate)
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command=command)),
            msg("好的，到此为止"),
        ])
        events = list(agent.run("执行命令"))
        return events, journal, agent

    def _backfill_of(self, events):
        payload = next(p for k, p in events if k == "tool_result")
        self.assertEqual(payload["name"], "run_bash")
        return json.loads(payload["result"])

    def test_ask_commands_pause_and_deny_without_executing(self):
        """复合(;)、多空格、flag 拆写、换行分隔、git push —— 全部命中 ask：
        先弹 permission_request（载荷完整），停驻期间探针零执行，超时后按
        拒绝回填（带原因与 hint），历史里留下的是拒绝结果而非执行结果。"""
        for command in ("ls;rm -rf /", "rm  -rf", "rm -r -f",
                        "echo a\nsudo x", "git push origin main"):
            with self.subTest(command=command):
                events, journal, agent = self._run_round(command)
                reqs = [p for k, p in events if k == "permission_request"]
                self.assertEqual(len(reqs), 1, f"{command!r} 应恰有一张确认卡")
                self.assertEqual(reqs[0]["tool"], "run_bash")
                self.assertEqual(reqs[0]["input"], {"command": command})
                self.assertTrue(reqs[0]["reason"] and reqs[0]["id"])
                self.assertEqual(journal, [], "停驻/拒绝期间工具绝不能被执行")
                payload = self._backfill_of(events)
                self.assertFalse(payload["ok"])
                self.assertTrue(payload["error"].startswith("权限拒绝: "))
                self.assertIn("hint", payload)
                tool_msgs = [m for m in agent.history if m["role"] == "tool"]
                self.assertEqual(len(tool_msgs), 1)  # 拒绝也必须正式回填（配对不悬空）
                self.assertTrue(any(k == "done" for k, _ in events))

    def test_quoted_and_pushish_run_normally(self):
        """引号内的 sudo 只是 echo 的参数、pushish 是另一个词：不误伤，
        照常执行（探针收到原命令、结果 ok）。"""
        for command in ("echo 'sudo rm -rf /'", "git pushish"):
            with self.subTest(command=command):
                events, journal, agent = self._run_round(command)
                self.assertFalse([k for k, _ in events if k == "permission_request"],
                                 f"{command!r} 不应弹确认卡")
                self.assertEqual(journal, [command])
                payload = self._backfill_of(events)
                self.assertTrue(payload["ok"])


# ---------------------------------------------------------------------------
# 四、并发写
# ---------------------------------------------------------------------------

class TestConcurrentSave(unittest.TestCase):
    """防的事故：落盘并发/迟到写导致数据损坏——同一行被写两份（重复 mid）、
    显示序断裂或错位、指纹账本互踩引发全表重写、会话删除后迟到写入留下
    孤儿消息行。

    并发契约说明：生产环境同一会话的回合由会话锁串行执行（app.py worker），
    save_messages 不承诺"同会话两路【不相交】的新增消息并发落盘不撞 ord"——
    那是会话锁的职责。这里测的是它自己承诺的并发面：同一份历史被两个线程
    同时落盘（重复落盘的竞态）必须幂等收敛。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_db_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.addCleanup(setattr, db, "DB_PATH", self._orig)
        db.renumbered_sessions.clear()
        self.addCleanup(db.renumbered_sessions.clear)

    def test_two_threads_same_history_idempotent(self):
        """两个线程同会话并发 save_messages（同一份历史、各自指纹账本）：
        无重复 mid、ord 单调无并列、两本账本算出的指纹一致且与库内重建的
        一致——账本互踩的表现就是重启后误判全变、全表重写。"""
        base = [user(f"消息{i}") if i % 2 == 0 else asst(f"回答{i}") for i in range(6)]
        shared_mids = [uuid.uuid4().hex for _ in base]  # 同一逻辑消息在两份副本里同 mid
        histories = []
        for _ in range(2):  # 每线程一份独立副本（模拟两个 worker 各持一份反序列化历史）
            hist = [dict(m) for m in base]
            for m, mid in zip(hist, shared_mids):
                m["_mid"] = mid
            histories.append(hist)

        barrier = threading.Barrier(2)
        errors, ledgers = [], []

        def worker(hist):
            try:
                saved = {}
                barrier.wait()
                db.save_messages("s1", hist, saved)
                ledgers.append(saved)
            except Exception as e:  # pragma: no cover - 失败要浮出水面
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(h,)) for h in histories]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        msgs = db.get_messages("s1")
        self.assertEqual(db.count_messages("s1"), 6, "主键幂等：6 行不多不少")
        mids = [m["_mid"] for m in msgs]
        self.assertEqual(len(mids), len(set(mids)), "无重复 mid")
        ords = [m["_ord"] for m in msgs]
        self.assertEqual(ords, sorted(ords))
        self.assertEqual(len(ords), len(set(ords)), "ord 无并列（断裂的前兆）")
        self.assertEqual(len(ledgers), 2)
        self.assertEqual(ledgers[0], ledgers[1], "两本账本必须一致")
        self.assertEqual(ledgers[0], db.fingerprints(msgs), "账本与库内重建一致")
        # 幂等收尾：任一账本再存一次都是 0 写入
        self.assertEqual(db.save_messages("s1", histories[0], ledgers[0]), 0)

    def test_late_write_after_session_delete_leaves_no_orphans(self):
        """会话删除后的迟到写入：直接放弃，不留孤儿消息行、不留归档目录。"""
        history = [user("早到的消息")]
        saved = {}
        self.assertEqual(db.save_messages("s1", history, saved), 1)
        big = {"role": "assistant", "content": "巨" * (db.MAX_INLINE_BYTES + 1)}
        history.append(big)
        self.assertEqual(db.save_messages("s1", history, saved), 1)
        self.assertTrue((self.tmp / "artifacts" / "s1").exists())

        deleted = threading.Event()
        go_ahead = threading.Event()
        results = {}

        def late_writer():
            deleted.wait(5)          # 等主线程删除会话
            results["written"] = db.save_messages("s1", history, saved)
            go_ahead.set()

        t = threading.Thread(target=late_writer)
        t.start()
        db.delete_session("s1")
        deleted.set()
        go_ahead.wait(5)
        t.join()

        self.assertEqual(results["written"], 0, "迟到写入必须放弃")
        self.assertEqual(db.count_messages("s1"), 0, "不留孤儿消息行")
        self.assertFalse((self.tmp / "artifacts" / "s1").exists(), "归档随会话清理")

    def test_concurrent_saves_survive_wal_without_corruption(self):
        """并发落盘后库文件完整性：integrity_check 必须 ok（WAL 模式下
        两个连接交错提交不得产生半提交状态）。"""
        base = [user("A"), asst("B")]
        shared_mids = [uuid.uuid4().hex for _ in base]
        hists = []
        for _ in range(2):
            h = [dict(m) for m in base]
            for m, mid in zip(h, shared_mids):
                m["_mid"] = mid
            hists.append(h)
        barrier = threading.Barrier(2)

        def worker(h):
            barrier.wait()
            db.save_messages("s1", h, {})

        ts = [threading.Thread(target=worker, args=(h,)) for h in hists]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(db.count_messages("s1"), 2)


# ---------------------------------------------------------------------------
# 五、压缩安全回归
# ---------------------------------------------------------------------------

class TestCompactSafety(unittest.TestCase):
    """防的事故：压缩把边界插进悬空的 tool 配对中间——保留段以 tool 结果
    开头、或被压缩段以带 tool_calls 的 assistant 结尾，下一轮请求都会被
    服务商直接 400（对话直接坏掉）；连续两次压缩若同时出现新旧两个摘要、
    或丢掉首条用户消息锚点，模型会看到互相矛盾/失忆的历史。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_compact_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    class SummaryLLM:
        """每次总结调用弹出剧本里的下一条摘要。"""

        def __init__(self, summaries):
            self.summaries = list(summaries)
            self.calls = []

        def chat_stream(self, messages, tools=None, cancel=None):
            self.calls.append(messages)
            yield "message", {"role": "assistant", "content": self.summaries.pop(0)}

    def _agent(self, llm):
        agent = Agent(llm=llm, verbose=False, workspace=str(self.tmp), context_window=300)
        agent.cancel_event = threading.Event()
        return agent

    def _long_history(self):
        h = [user("原始需求：重构支付模块")]
        for i in range(6):
            h.append(asst("长内容" * 120 + str(i)))
        h.append(tool_call_asst("c1", "run_bash", command="python3 pay.py"))
        h.append(tool_result("运行通过", "c1"))
        for i in range(6, 14):
            h.append(asst("长内容" * 120 + str(i)))
        return h

    def test_boundary_never_lands_inside_tool_pair(self):
        """首次压缩：边界两侧都不得破坏 tool 配对（下一条不是 tool 结果、
        上一条不是悬空调用），且历史只多出一条边界标记。"""
        llm = self.SummaryLLM(["第一次摘要：完成支付模块初版"])
        agent = self._agent(llm)
        agent.history = self._long_history()
        before = list(agent.history)

        result = agent._maybe_compact()
        self.assertIsNotNone(result)
        idx = [i for i, m in enumerate(agent.history) if m["role"] == "compact"]
        self.assertEqual(len(idx), 1)
        i = idx[0]
        self.assertEqual(agent.history[:i], before[:i], "边界之前的原文一字不动")
        self.assertEqual(agent.history[i + 1:], before[i:], "边界之后的原文一字不动")
        self.assertFalse(agent.history[i - 1].get("tool_calls"),
                         "被压缩段不得以悬空的工具调用结尾")
        self.assertNotEqual(agent.history[i + 1].get("role"), "tool",
                            "保留段不得以孤儿工具结果开头")

    def test_double_compact_only_last_boundary_and_old_summary_merged(self):
        """连续两次压缩：模型视图只认最后一条边界（旧摘要不再出现），但
        第二次总结的输入里必须包含旧摘要（旧摘要被吸收而非丢弃）；首条
        用户消息锚点逐字保留；保留段完整到最新消息。"""
        llm = self.SummaryLLM(["第一次摘要：完成支付模块初版",
                               "第二次摘要：支付模块初版完成，边界用例待补"])
        agent = self._agent(llm)
        agent.history = self._long_history()

        self.assertIsNotNone(agent._maybe_compact())
        # 压缩后继续对话（新增内容落在边界之后）
        agent.history.append(user("边界之后的新问题"))
        agent.history.append(tool_call_asst("c2", "run_bash", command="python3 t.py"))
        agent.history.append(tool_result(" PASS ", "c2"))
        for i in range(4):
            agent.history.append(asst("新阶段内容" * 120 + str(i)))

        self.assertIsNotNone(agent._maybe_compact())
        self.assertEqual(len([m for m in agent.history if m["role"] == "compact"]), 2)
        # 旧摘要进了第二次总结的输入（吸收，不是丢弃）
        second_prompt = llm.calls[1][0]["content"]
        self.assertIn("第一次摘要", second_prompt)

        view = agent._messages_for_model()
        self.assertEqual(view[0]["content"], "原始需求：重构支付模块", "锚点逐字保留")
        joined = json.dumps(view, ensure_ascii=False)
        self.assertIn("第二次摘要", joined)
        self.assertNotIn("第一次摘要", joined, "旧摘要不得与新摘要同时出现在视图里")
        self.assertEqual(view[1]["role"], "user")
        self.assertNotEqual(view[2].get("role"), "tool", "视图摘要段之后不得悬空")
        # 保留段覆盖到最新消息
        self.assertEqual(view[-1]["content"], "新阶段内容" * 120 + "3")
        # 视图里没有任何 role=compact 标记或内部字段泄漏给服务商
        self.assertFalse(any(m["role"] == "compact" for m in view))
        self.assertFalse(any(k.startswith("_") for m in view for k in m))
        # 悬空配对总检：视图内每个 tool 结果的前一条必须是对应的调用请求
        call_ids = set()
        for m in view:
            if m["role"] == "assistant":
                for c in m.get("tool_calls") or []:
                    call_ids.add(c["id"])
            elif m["role"] == "tool":
                self.assertIn(m["tool_call_id"], call_ids, "tool 结果必须紧跟其调用")

    def test_compact_llm_failure_leaves_history_intact(self):
        """总结调用失败：历史一字不动、不抛异常（压缩是锦上添花，绝不
        打断会话——失败的最坏结果只是这一轮没压缩）。"""

        class Boom:
            def chat_stream(self, messages, tools=None, cancel=None):
                raise RuntimeError("网络错误")
                yield  # pragma: no cover

        agent = self._agent(Boom())
        agent.history = self._long_history()
        n = len(agent.history)
        self.assertIsNone(agent._maybe_compact())
        self.assertEqual(len(agent.history), n)
        self.assertFalse(any(m["role"] == "compact" for m in agent.history))


# ---------------------------------------------------------------------------
# 六、记忆安全回归
# ---------------------------------------------------------------------------

class TestMemorySafety(unittest.TestCase):
    """防的事故：轮末提取把模型产出的非法内容写进磁盘——路径逃逸写到
    记忆目录之外、frontmatter 残缺的记忆污染注入链路、超长正文撑爆上下文，
    以及最隐蔽的"半套记忆"：一批操作写了一半失败，正文与索引不一致。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_mem_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mem_dir = self.tmp / ".agent-memory"
        self.mem_dir.mkdir()

    def _write_op(self, fname="user-pref.md", **over):
        op = {"action": "write", "file": fname,
              "frontmatter": {"name": fname[:-3], "description": "一句话摘要",
                              "metadata": {"type": "user"}},
              "body": "正文"}
        op.update(over)
        return op

    def _snapshot(self) -> dict:
        out = {}
        for p in sorted(self.mem_dir.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(self.mem_dir))] = p.read_bytes()
        return out

    def test_valid_write_lands_with_index(self):
        out = apply_extraction(self.mem_dir, {"memories": [self._write_op()]})
        self.assertIsNone(out["abandoned"])
        self.assertEqual(out["written"], ["user-pref.md"])
        text = (self.mem_dir / "user-pref.md").read_text(encoding="utf-8")
        self.assertIn("name: user-pref", text)
        self.assertIn("[user-pref](user-pref.md)",
                      (self.mem_dir / "MEMORY.md").read_text(encoding="utf-8"))

    def test_non_kebab_and_bad_structure_rejected(self):
        """非 kebab-case 文件名、frontmatter 缺 description/type、body 非字符串：
        逐条拒绝且磁盘零变化。"""
        bad_ops = [
            self._write_op(fname="UserPref.md"),
            self._write_op(fname="user_pref.md"),
            self._write_op(frontmatter={"name": "x", "metadata": {"type": "user"}}),
            self._write_op(frontmatter={"name": "x", "description": "d"}),
            self._write_op(frontmatter={"name": "x", "description": "d",
                                        "metadata": {"type": "secret"}}),
            self._write_op(body=None),
            {"action": "patch", "file": "a.md"},  # 非法 action
        ]
        before = self._snapshot()
        for op in bad_ops:
            out = apply_extraction(self.mem_dir, {"memories": [op]})
            self.assertTrue(out["abandoned"], f"应拒绝 {op!r}")
        self.assertEqual(self._snapshot(), before, "磁盘一字节不动")

    def test_body_over_8000_truncated(self):
        body = "长" * 9000
        out = apply_extraction(self.mem_dir, {"memories": [self._write_op(body=body)]})
        self.assertIsNone(out["abandoned"])
        text = (self.mem_dir / "user-pref.md").read_text(encoding="utf-8")
        self.assertIn("长" * 8000, text)
        self.assertNotIn("长" * 8001, text)
        self.assertLess(len(text), 8200)

    def test_all_or_nothing_on_mixed_batch(self):
        """混合批次里一条非法：整批放弃，合法的那条也不得落盘——半套记忆
        （正文写了、索引没跟上）比没有更糟。"""
        good = self._write_op(fname="good-one.md")
        bad = self._write_op(fname="../evil.md")
        before = self._snapshot()
        out = apply_extraction(self.mem_dir, {"memories": [good, bad]})
        self.assertTrue(out["abandoned"])
        self.assertEqual(out["written"], [])
        self.assertEqual(self._snapshot(), before, "整批放弃，磁盘一字节不动")

    def test_delete_syncs_index_and_idempotent(self):
        apply_extraction(self.mem_dir, {"memories": [self._write_op()]})
        out = apply_extraction(self.mem_dir,
                               {"memories": [{"action": "delete", "file": "user-pref.md"}]})
        self.assertEqual(out["deleted"], ["user-pref.md"])
        self.assertFalse((self.mem_dir / "user-pref.md").exists())
        self.assertNotIn("user-pref", (self.mem_dir / "MEMORY.md").read_text(encoding="utf-8"))
        # 再删一次：幂等成功且不再报已删除
        out2 = apply_extraction(self.mem_dir,
                                {"memories": [{"action": "delete", "file": "user-pref.md"}]})
        self.assertIsNone(out2["abandoned"])
        self.assertEqual(out2["deleted"], [])


# ---------------------------------------------------------------------------
# 七、权限时机回归（跨组件：Agent + 闸门 + db 持久化）
# ---------------------------------------------------------------------------

class TestPermissionTiming(unittest.TestCase):
    """防的事故：ask 等待与执行调度时序错乱——停驻期间工具被偷跑（越权
    执行用户还没确认的高危操作）、事件载荷残缺导致前端卡片画不出来、
    恢复后结果回填错位（结果安到别的调用头上）、超时/停止没按拒绝收场
    （挂死或放行）。与 test_permissions 互补：这里走 Agent 全链路并把
    回合历史落库，验证持久化后的时间线与回填一致。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec_timing_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.addCleanup(setattr, db, "DB_PATH", self._orig_db)
        db.renumbered_sessions.clear()
        self.addCleanup(db.renumbered_sessions.clear)

        import tools
        self._tools = tools
        self.journal = []
        self._orig_run_bash = tools.TOOL_REGISTRY["run_bash"]

        def probe(command="", ctx=None):
            self.journal.append(command)
            return json.dumps({"ok": True, "ran": command}, ensure_ascii=False)

        tools.TOOL_REGISTRY["run_bash"] = probe
        self.addCleanup(lambda: tools.TOOL_REGISTRY.__setitem__("run_bash", self._orig_run_bash))

        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        (self.ws / "a.txt").write_text("文件内容", encoding="utf-8")

    def _agent(self, ask_timeout=60.0):
        gate = PermissionGate(self.ws.resolve(), ask_timeout=ask_timeout)
        agent = Agent(llm=ScriptedLLM([]), verbose=False, workspace=str(self.ws),
                      permission_gate=gate)
        return agent

    def test_ask_pauses_with_zero_execution_and_full_payload(self):
        """ask 停驻：探针零执行、permission_request 载荷完整（id/tool/input/
        reason）、此刻历史里尚无任何工具结果（回填只发生在调度阶段）。"""
        agent = self._agent()
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main"),
                              call("r1", "read_file", path="a.txt")),
            msg("完成"),
        ])
        events = []
        req = None
        for kind, payload in agent.run("推上去"):
            events.append((kind, payload))
            if kind == "permission_request":
                req = payload
                break
        self.assertIsNotNone(req)
        self.assertEqual(req["tool"], "run_bash")
        self.assertEqual(req["input"], {"command": "git push origin main"})
        self.assertTrue(req["id"] and req["reason"])
        self.assertEqual(self.journal, [], "停驻期间探针零执行")
        self.assertEqual([m for m in agent.history if m["role"] == "tool"], [])

    def test_resume_backfills_in_request_order_and_persists(self):
        """恢复后按请求顺序回填（run_bash 在前、read_file 在后），回合历史
        落库后读回的 tool 消息顺序与请求一致——错位会把结果安到别的调用上。"""
        agent = self._agent()
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main"),
                              call("r1", "read_file", path="a.txt")),
            msg("完成"),
        ])
        gen = agent.run("推上去")
        req = None
        for kind, payload in gen:
            if kind == "permission_request":
                req = payload
                break
        t = threading.Thread(target=agent.resolve_permission,
                             args=(req["id"], "allow_session"))
        t.start()
        events = list(gen)
        t.join()
        self.assertEqual(self.journal, ["git push origin main"])
        results = [p for k, p in events if k == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertIn("git push origin main", results[0]["result"])
        self.assertIn("文件内容", results[1]["result"])
        # 持久化后时间线一致：db 里的 tool 消息顺序 = 请求顺序，配对完整
        saved = {}
        db.save_messages("s1", agent.history, saved)
        msgs = db.get_messages("s1")
        self.assertEqual([m["tool_call_id"] for m in msgs if m["role"] == "tool"],
                         ["b1", "r1"])

    def test_timeout_resolves_as_deny_end_to_end(self):
        """超时：无人应答 → 等待及时解除、按拒绝回填、探针零执行、回合正常
        收尾（done），拒绝结果持久化进库（重启后模型/前端看到的仍是拒绝）。"""
        agent = self._agent(ask_timeout=0.2)
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="sudo rm -rf /tmp/x")),
            msg("已放弃"),
        ])
        events = list(agent.run("清掉它"))
        payload = json.loads(next(p["result"] for k, p in events
                                  if k == "tool_result" and p["name"] == "run_bash"))
        self.assertFalse(payload["ok"])
        self.assertIn("权限拒绝", payload["error"])
        self.assertEqual(self.journal, [])
        self.assertTrue(any(k == "done" for k, _ in events))
        saved = {}
        db.save_messages("s1", agent.history, saved)
        persisted = [m for m in db.get_messages("s1") if m["role"] == "tool"]
        self.assertEqual(len(persisted), 1)
        self.assertIn("权限拒绝", persisted[0]["content"])

    def test_stop_while_pending_resolves_as_deny(self):
        """等待确认时用户点停止：按拒绝收场、探针零执行、回合收尾不挂死。"""
        agent = self._agent(ask_timeout=60.0)
        agent.llm = ScriptedLLM([
            tool_call_message(call("b1", "run_bash", command="git push origin main")),
            msg("好的，不推了"),
        ])
        gen = agent.run("推上去")
        for kind, _ in gen:
            if kind == "permission_request":
                break
        agent.stop()
        events = list(gen)
        payload = json.loads(next(p["result"] for k, p in events
                                  if k == "tool_result"))
        self.assertFalse(payload["ok"])
        self.assertIn("权限拒绝", payload["error"])
        self.assertEqual(self.journal, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
