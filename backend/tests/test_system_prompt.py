"""
系统提示词与工具呈现质量的单元测试（cd backend && python3 -m unittest tests.test_system_prompt -v)
====================================================================================================

锁定本次「提示词与工具呈现」改造的三条主干不变式：
1. SYSTEM_PROMPT 包含记忆契约，且是 import 而非副本——修改 memory.MEMORY_CONTRACT
   后拼接结果同步变化（两份副本必然漂移，import 才能单一来源）；Agent 的 system
   组装里契约恰好出现一次（既不重复注入，也不会因迁移而漏掉）。
2. read_file 行号化输出：右对齐行号 + 制表符 + 原文（行号从 1 起），offset/limit
   分段与 2000 行上限的截断提示文案；grep 与 read_file 共用同一行号格式。
3. 工具结果统一信封：成功 {ok:true, result,...}、失败 {ok:false, error, hint?}——
   抛异常的工具与正常工具返回结构一致；权限拒绝（rejection_result）同构。

全部为纯逻辑测试（临时工作区 + 直接调工具函数 / execute_tool），不发网络请求。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import agent as agent_mod
import memory
import system_prompt as sp
from agent import Agent
from memory import MEMORY_CONTRACT
from permissions import DENY, Verdict, rejection_result
from system_prompt import SYSTEM_PROMPT, build_system_prompt
from tools import TOOL_REGISTRY, TOOL_READ_ONLY, ToolContext, execute_tool


# ---------------------------------------------------------------------------
# 一、SYSTEM_PROMPT 与记忆契约（import 而非副本）
# ---------------------------------------------------------------------------

class TestPromptContract(unittest.TestCase):

    def test_system_prompt_contains_contract(self):
        """契约以 import 来源拼在 SYSTEM_PROMPT 末尾（快照含当前契约原文）。"""
        self.assertIn(memory.MEMORY_CONTRACT, SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.endswith(memory.MEMORY_CONTRACT))

    def test_contract_is_imported_not_copied(self):
        """验收点：修改契约常量后拼接结果同步变化。

        system_prompt.build_system_prompt 通过 memory.MEMORY_CONTRACT 模块属性
        取契约——若实现是复制一份文本副本，patch 常量后重建的提示词不会包含
        新文本，这个断言就会失败（这正是要防的漂移）。"""
        sentinel = "【测试契约】THIS_IS_A_MODIFIED_CONTRACT_XYZ"
        old = memory.MEMORY_CONTRACT
        try:
            memory.MEMORY_CONTRACT = sentinel
            rebuilt = sp.build_system_prompt()
            self.assertIn(sentinel, rebuilt)
            self.assertIn("你是一个在本地工作区里工作的编程助手", rebuilt)  # 正文不丢
        finally:
            memory.MEMORY_CONTRACT = old
        # 恢复后重建回到原文（确认上面不是残留状态）
        self.assertNotIn(sentinel, sp.build_system_prompt())
        self.assertIn(memory.MEMORY_CONTRACT, sp.build_system_prompt())

    def test_agent_uses_the_same_constant(self):
        """agent.py 引用的是同一个常量对象（is 而非值相等的副本）。"""
        self.assertIs(agent_mod.SYSTEM_PROMPT, sp.SYSTEM_PROMPT)

    def test_agent_system_content_has_contract_exactly_once_and_index(self):
        """Agent 组装的 system：契约恰好一次 + 动态索引在座 + 历史绝无契约。

        契约从 memory 块迁到 SYSTEM_PROMPT 后，最怕两件事：迁移后漏注入
        （旧 system_memory_block 还在被别处拼一次）或重复注入（块里还留着
        契约）。count == 1 同时锁死两个方向。"""
        ws = Path(tempfile.mkdtemp(prefix="prompt_ws_"))
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=_StaticLLM(), verbose=False, workspace=str(ws))
        system = agent._system_content()
        self.assertEqual(system.count(memory.MEMORY_CONTRACT), 1)
        self.assertIn("用户记忆索引（跨会话持久）", system)
        self.assertIn("（暂无记忆）", system)  # 空 workspace 的降级文案


class _StaticLLM:
    """Agent 构造冒烟用的假客户端（不发起真实请求）。"""

    def chat_stream(self, messages, tools=None, cancel=None):
        yield "message", {"role": "assistant", "content": "ok"}


# ---------------------------------------------------------------------------
# 二、read_file 行号化 + offset/limit 分段
# ---------------------------------------------------------------------------

TOTAL_LINES = 2500  # 超过 MAX_READ_LINES(2000)：默认读取必须触发截断提示


class ReadFileTestBase(unittest.TestCase):

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="prompt_ws_"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.ctx = ToolContext(workspace=self.ws)
        # 行内容 = line<N>：行号与内容互相可校验
        lines = [f"line{i}" for i in range(1, TOTAL_LINES + 1)]
        (self.ws / "sample.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 小文件：不触发截断，供纯格式断言
        (self.ws / "small.py").write_text(
            "\n".join(f"line{i}" for i in range(1, 21)) + "\n", encoding="utf-8")

    def read(self, path="sample.py", **kw):
        return json.loads(execute_tool("read_file", {"path": path, **kw}, self.ctx))


class TestReadFileNumbering(ReadFileTestBase):

    def test_lines_are_numbered_with_tab_from_one(self):
        """验收格式："  17\tdef foo():" —— 右对齐行号 + 制表符 + 原文，行号从 1 起。"""
        r = self.read(path="small.py")
        self.assertTrue(r["ok"])
        self.assertEqual(r["shown"], [1, 20])
        out = r["result"].splitlines()
        self.assertEqual(len(out), 20)
        self.assertEqual(out[0], f"{1:>4}\tline1")
        self.assertEqual(out[16], f"{17:>4}\tline17")  # 题面示例的 17 行
        self.assertEqual(out[-1], f"{20:>4}\tline20")
        for i, ln in enumerate(out, 1):
            self.assertTrue(ln.startswith(f"{i:>4}\t"), f"第 {i} 行格式不对: {ln!r}")

    def test_offset_and_limit_window(self):
        """offset/limit 分段：返回区间与请求一致，行号接续全局行号（不从头计数）。
        窗口没读到文件尾时统一带续读提示（模型因此知道"下面还有"及怎么续读），
        提示行不算正文行。"""
        r = self.read(path="small.py", offset=5, limit=10)
        self.assertEqual(r["shown"], [5, 14])
        out = r["result"].splitlines()
        self.assertEqual(len(out), 11)  # 10 行正文 + 1 行续读提示
        self.assertTrue(out[0].startswith(f"{5:>4}\tline5"))
        self.assertTrue(out[-2].startswith(f"{14:>4}\tline14"))
        self.assertIn("用 offset/limit 读取后续段", out[-1])
        self.assertIn("offset=15", out[-1])
        # 读到文件尾（含最后一行）就不提示：小文件全读无提示
        whole = self.read(path="small.py")
        self.assertNotIn("已截断", whole["result"])
        big = self.read(offset=120, limit=40)  # 大文件上的窗口：必然伴随截断提示
        self.assertEqual(big["shown"], [120, 159])
        self.assertEqual(len(big["result"].splitlines()), 41)
        self.assertIn("已截断", big["result"].splitlines()[-1])

    def test_truncation_notice_points_to_offset(self):
        """超 2000 行：截断 + 尾部明确提示「用 offset/limit 读取后续段」并给出续读位置。"""
        r = self.read()  # 默认全读 → 撞 MAX_READ_LINES
        self.assertTrue(r["ok"])
        self.assertEqual(r["total_lines"], TOTAL_LINES)
        self.assertEqual(r["shown"], [1, 2000])
        tail = r["result"].splitlines()[-1]
        self.assertIn("用 offset/limit 读取后续段", tail)
        self.assertIn("offset=2001", tail)
        self.assertIn("已截断", tail)

    def test_limit_smaller_than_file_truncates_with_notice(self):
        """显式 limit 截短与行数上限共用同一条截断提示路径（提示行不算正文行）。"""
        r = self.read(offset=2000, limit=3)
        self.assertEqual(r["shown"], [2000, 2002])
        self.assertEqual(len(r["result"].splitlines()), 4)  # 3 行正文 + 1 行提示
        self.assertIn("用 offset/limit 读取后续段", r["result"].splitlines()[-1])
        self.assertIn("offset=2003", r["result"])

    def test_offset_beyond_eof_returns_error_with_guidance(self):
        """offset 超出文件末尾：明确报错并建议用 grep 定位（而不是返回空结果让模型困惑）。"""
        r = self.read(offset=TOTAL_LINES + 10)
        self.assertFalse(r["ok"])
        self.assertIn("读取区间为空", r["error"])
        self.assertIn("grep", r["hint"])

    def test_bad_params_are_tolerated(self):
        """模型传来的 offset/limit 可能是字符串/0/负数：宽容归一而不是报错打断任务。"""
        r = self.read(offset=0, limit="10")
        self.assertEqual(r["shown"], [1, 10])
        r2 = self.read(offset=-3, limit=2)
        self.assertEqual(r2["shown"], [1, 2])
        r3 = self.read(offset="abc")  # offset 归一为 1、limit 归一为不限 → 走正常截断路径
        self.assertTrue(r3["ok"])
        self.assertEqual(r3["shown"], [1, 2000])

    def test_missing_file_error_carries_hint(self):
        r = json.loads(execute_tool("read_file", {"path": "nope.py"}, self.ctx))
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])
        self.assertIn("list_dir", r["hint"])


# ---------------------------------------------------------------------------
# 三、grep 与 read_file 共用行号格式；apply_patch 锚定与救援
# ---------------------------------------------------------------------------

class TestGrepAndPatch(ReadFileTestBase):

    def setUp(self):
        super().setUp()
        (self.ws / "sample.py").write_text(
            "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
            encoding="utf-8")

    def test_grep_uses_same_numbered_format(self):
        """grep 的 text 字段与 read_file 同格式（右对齐行号+制表符），行号单列可机读。"""
        r = json.loads(execute_tool("grep", {"pattern": "return a", "path": "sample.py"}, self.ctx))
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 2)
        self.assertEqual([m["line"] for m in r["result"]], [2, 6])
        self.assertEqual(r["result"][0]["text"], f"{2:>4}\t    return a - b")
        self.assertEqual(r["result"][1]["file"], "sample.py")

    def test_patch_exact_anchor_from_read_file_output(self):
        """正常链路：read_file 带行号原文 → 模型剥行号后逐字符锚定 → 精确替换。"""
        r = json.loads(execute_tool(
            "apply_patch", {"path": "sample.py", "search": "    return a - b",
                            "replace": "    return a + b"}, self.ctx))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["matched_lines"], [2, 2])
        self.assertIn("return a + b", (self.ws / "sample.py").read_text(encoding="utf-8"))

    def test_patch_rescues_wrong_indent_and_lineno_pollution(self):
        """锚定救援：search 缩进错 / 带行号栏，仍能按文件缩进对齐替换（不整次失败）。"""
        # 缩进错：search 用 2 空格，文件是 4 空格
        r = json.loads(execute_tool(
            "apply_patch", {"path": "sample.py",
                            "search": "def add(a, b):\n  return a - b",
                            "replace": "def add(a, b):\n  return a + b"}, self.ctx))
        self.assertTrue(r["ok"], r)
        self.assertIn("note", r)  # 救援必须留痕，模型知道实际发生的是对齐替换
        text = (self.ws / "sample.py").read_text(encoding="utf-8")
        self.assertIn("    return a + b", text)
        # 行号栏污染：模型把 "  2\t" 一起复制进了 search/replace
        r2 = json.loads(execute_tool(
            "apply_patch", {"path": "sample.py",
                            "search": "  2\t  return a + b",
                            "replace": "  2\treturn a + b"}, self.ctx))
        self.assertTrue(r2["ok"], r2)
        self.assertNotIn("\t", (self.ws / "sample.py").read_text(encoding="utf-8").splitlines()[1])

    def test_patch_ambiguity_and_wrong_content_fail_with_hint(self):
        """多义与记错内容必须失败，且失败信封带可执行的 hint——宁失败不错改。"""
        (self.ws / "sample.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        r = json.loads(execute_tool(
            "apply_patch", {"path": "sample.py", "search": "x = 1", "replace": "x = 2"}, self.ctx))
        self.assertFalse(r["ok"])
        self.assertIn("无法定位", r["error"])
        self.assertIn("hint", r)
        (self.ws / "sample.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        r2 = json.loads(execute_tool(
            "apply_patch", {"path": "sample.py", "search": "return a * b", "replace": "return a + b"}, self.ctx))
        self.assertFalse(r2["ok"])
        self.assertIn("read_file", r2["hint"])


# ---------------------------------------------------------------------------
# 四、统一信封
# ---------------------------------------------------------------------------

class TestUnifiedEnvelope(unittest.TestCase):

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="prompt_ws_"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.ctx = ToolContext(workspace=self.ws)
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8")

    def test_normal_and_exploding_tools_share_structure(self):
        """验收点：正常工具与抛异常工具返回结构一致——ok 恒在，成功有 result，
        失败有 error（可选 hint），模型只需要学一种结果形状。"""
        def boom(**kw):
            raise RuntimeError("内部炸了")

        TOOL_REGISTRY["__boom__"] = boom
        TOOL_READ_ONLY["__boom__"] = True
        self.addCleanup(lambda: (TOOL_REGISTRY.pop("__boom__", None),
                                 TOOL_READ_ONLY.pop("__boom__", None)))
        good = json.loads(execute_tool("read_file", {"path": "a.txt"}, self.ctx))
        bad = json.loads(execute_tool("__boom__", {}, self.ctx))
        self.assertTrue(good["ok"])
        self.assertIn("result", good)
        self.assertFalse(bad["ok"])
        self.assertIn("error", bad)
        self.assertIn("内部炸了", bad["error"])
        self.assertNotIn("result", bad)
        # 键的形状一致：ok/result/error 之外不引入第三种结果形态
        self.assertEqual(set(good) - {"path", "total_lines", "shown"},
                         {"ok", "result"})

    def test_every_builtin_tool_returns_envelope(self):
        """全量扫一遍 10 个注册工具：成功带 result、失败带 error，无一例外。"""
        (self.ws / "b.py").write_text("def hi():\n    return 1\n", encoding="utf-8")
        cases = [
            ("read_file", {"path": "b.py"}),
            ("write_file", {"path": "new.txt", "content": "x"}),
            ("apply_patch", {"path": "b.py", "search": "return 1", "replace": "return 2"}),
            ("list_dir", {}),
            ("grep", {"pattern": "hi"}),
            ("run_bash", {"command": "echo envelopecase"}),
            ("calculator", {"expression": "6*7"}),
            ("current_time", {}),
            ("get_weather", {"city": "北京"}),
            ("analyze_image", {}),  # 无图片 → 失败信封（也必须是统一形状）
        ]
        for name, args in cases:
            payload = json.loads(execute_tool(name, args, self.ctx))
            self.assertIn("ok", payload, name)
            self.assertIsInstance(payload["ok"], bool, name)
            if payload["ok"]:
                self.assertIn("result", payload, name)
            else:
                self.assertIn("error", payload, name)

    def test_run_bash_nonzero_exit_is_failure_envelope_with_output(self):
        """run_bash 非零退出：ok=false + error，但 stdout/stderr 原样保留——
        命令失败不是工具失败，输出是模型定位问题的第一手材料。"""
        payload = json.loads(execute_tool("run_bash", {"command": "printf x; exit 7"}, self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 7)
        self.assertIn("7", payload["error"])
        self.assertIn("hint", payload)
        self.assertEqual(payload["stdout"], "x")

    def test_permission_rejection_shares_same_envelope(self):
        """权限拒绝（permissions.rejection_result）与工具失败同构：ok/error/hint。"""
        payload = json.loads(rejection_result(Verdict(DENY, "递归强制删除", ("run_bash", "rm -rf"))))
        self.assertFalse(payload["ok"])
        self.assertIn("权限拒绝", payload["error"])
        self.assertIn("hint", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
