"""
结果落盘轻引用的单元测试（cd backend && python3 -m unittest tests.test_toolresult_ref -v）
==========================================================================================

覆盖 agent._externalize_tool_result（60k 截断闸的升级档）与 read_tool_result
工具的读回链路：
1. 落盘触发：超内联阈值的结果经 result_sink 落盘，历史里换成预览 + full 引用
   ——JSON 信封保留 ok/error/hint 语义键、只截 result 字段；纯文本直接预览+提示；
2. 阈值内原样返回；无 sink（CLI/单测口径）退回旧的 60k 截断；sink 抛异常同样退回；
3. read_tool_result 工具：行分段读回、路径穿越/绝对路径/非法后缀拒绝、
   空区间与阅读器未注入的可读报错；
4. 清理占位符升级：带 full 引用的旧结果被清理后，占位符带引用路径（从
   "必须重调工具"变成"指针还在、按需读盘"）；
5. 存储面：同内容写盘去重；删除会话连 tool_results/<sid>/ 一起清理。
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
from agent import (Agent, MAX_TOOL_RESULT_CHARS, TOOL_RESULT_EXTERNALIZE_CHARS,
                   TOOL_RESULT_PREVIEW_CHARS, CLEARED_TOOL_RESULT_PLACEHOLDER)
from tools import ToolContext, execute_tool


def make_agent(ws, **kw):
    class N:
        def chat_stream(self, messages, tools=None, cancel=None):
            yield "message", {"role": "assistant", "content": "ok"}
    return Agent(llm=N(), verbose=False, workspace=str(ws), **kw)


class StorageTestBase(unittest.TestCase):
    """临时库 + 临时工作区（db 函数在调用时读模块级 DB_PATH，重定向即隔离）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="toolresult_ref_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        self.ws = Path(self.tmp) / "ws"
        self.ws.mkdir()

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    def row(self, sql, *params):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute(sql, params).fetchone()


class TestExternalize(StorageTestBase):
    def backfill(self, result):
        agent = make_agent(self.ws,
                           result_sink=lambda content: db.write_tool_result("s1", content))
        kind, payload = agent._backfill_tool_result(
            {"id": "c1", "function": {"name": "read_file"}}, result)
        self.assertEqual(kind, "tool_result")
        return payload["result"]

    def test_oversized_json_envelope_gets_full_ref(self):
        body = "\n".join(f"L{i}" for i in range(1, 1200))  # 多行正文
        original = json.dumps({"ok": True, "path": "big.txt",
                               "result": body * 3}, ensure_ascii=False)
        self.assertGreater(len(original), TOOL_RESULT_EXTERNALIZE_CHARS)
        out = self.backfill(original)
        info = json.loads(out)  # 回填内容仍是合法信封
        self.assertTrue(info["ok"])
        self.assertEqual(info["path"], "big.txt")
        self.assertIn("read_tool_result", info["result"])
        self.assertIn(f"共 {len(body) * 3} 字符", info["result"])  # chars 指落盘正文
        # result 字段截到预览 + 提示；full 引用齐备且与盘上文件一致——
        # 盘上是信封的 result 正文（多行可读、可按行分页），不是 JSON 壳
        self.assertLessEqual(len(info["result"]),
                             TOOL_RESULT_PREVIEW_CHARS + 300)
        self.assertTrue(info["full"]["path"].startswith("s1/"))
        self.assertEqual(info["full"]["chars"], len(body) * 3)
        on_disk = db.read_tool_result(info["full"]["path"])
        self.assertEqual("\n".join(on_disk["lines"]), body * 3)
        self.assertGreater(on_disk["total_lines"], 100)  # 按行分页对它有效

    def test_error_envelope_keeps_error_and_hint(self):
        original = json.dumps({"ok": False, "error": "命令退出码 1",
                               "hint": "先读 stderr", "exit_code": 1,
                               "result": "Y" * (TOOL_RESULT_EXTERNALIZE_CHARS + 5)},
                              ensure_ascii=False)
        info = json.loads(self.backfill(original))
        self.assertEqual(info["error"], "命令退出码 1")  # 语义键不能因截断丢失
        self.assertEqual(info["hint"], "先读 stderr")
        self.assertTrue(info["full"]["path"])

    def test_oversized_plain_text_gets_preview_and_note(self):
        out = self.backfill("P" * (TOOL_RESULT_EXTERNALIZE_CHARS + 5))
        self.assertTrue(out.startswith("PPPP"))
        self.assertIn("read_tool_result", out)
        self.assertLess(len(out), TOOL_RESULT_PREVIEW_CHARS + 400)

    def test_within_threshold_untouched(self):
        result = json.dumps({"ok": True, "result": "small"}, ensure_ascii=False)
        self.assertEqual(self.backfill(result), result)

    def test_no_sink_falls_back_to_legacy_truncation(self):
        agent = make_agent(self.ws)  # CLI/单测口径：无 result_sink
        big = "Z" * (MAX_TOOL_RESULT_CHARS + 10)
        _, payload = agent._backfill_tool_result({"id": "c", "function": {"name": "t"}}, big)
        self.assertIn("已截断至前", payload["result"])
        self.assertIn(f"共 {len(big)} 字符", payload["result"])
        mid = "M" * 20_000  # 旧口径下 16k~60k 之间原样内联（不落盘也不截断）
        _, payload = agent._backfill_tool_result({"id": "c", "function": {"name": "t"}}, mid)
        self.assertEqual(payload["result"], mid)

    def test_sink_failure_falls_back_to_truncation(self):
        def boom(content):
            raise OSError("disk full")
        agent = make_agent(self.ws, result_sink=boom)
        big = "F" * (MAX_TOOL_RESULT_CHARS + 10)
        _, payload = agent._backfill_tool_result({"id": "c", "function": {"name": "t"}}, big)
        self.assertIn("已截断至前", payload["result"])


class TestReadToolResult(StorageTestBase):
    def setUp(self):
        super().setUp()
        self.ref = db.write_tool_result("s1", "\n".join(f"line {i}" for i in range(1, 51)))
        self.ctx = ToolContext()
        self.ctx.tool_result_reader = db.read_tool_result

    def call(self, **arguments):
        return json.loads(execute_tool("read_tool_result", arguments, self.ctx))

    def test_numbered_lines_and_paging(self):
        env = self.call(ref=self.ref["path"], offset=45, limit=10)
        self.assertTrue(env["ok"])
        self.assertEqual(env["total_lines"], 50)
        self.assertEqual(env["shown"], [45, 50])
        self.assertIn("  45\tline 45", env["result"])

    def test_path_traversal_rejected(self):
        for bad in ("../../x.txt", "/etc/passwd", "../db.sqlite", "s1/x.json"):
            env = self.call(ref=bad)
            self.assertFalse(env["ok"], bad)
            self.assertIn("ref", env["hint"])

    def test_empty_region_and_missing_reader(self):
        env = self.call(ref=self.ref["path"], offset=999)
        self.assertFalse(env["ok"])
        self.assertIn("读取区间为空", env["error"])
        bare = ToolContext()  # 阅读器未注入（CLI/异常部署）：可读报错，不崩
        env = json.loads(execute_tool("read_tool_result", {"ref": self.ref["path"]}, bare))
        self.assertFalse(env["ok"])
        self.assertIn("未配置", env["error"])


class TestClearedPlaceholder(StorageTestBase):
    def test_placeholder_keeps_full_ref(self):
        ref = db.write_tool_result("s1", "R" * 100)
        with_ref = json.dumps({"ok": True, "result": "P" * 5000,
                               "full": {"path": ref["path"], "bytes": ref["bytes"],
                                        "chars": ref["chars"]}}, ensure_ascii=False)
        plain = json.dumps({"ok": True, "result": "Q" * 5000}, ensure_ascii=False)
        agent = make_agent(self.ws)
        agent.history = [
            {"role": "user", "content": "go"},
            {"role": "tool", "tool_call_id": "c1", "content": with_ref},
            {"role": "user", "content": "again"},
            {"role": "tool", "tool_call_id": "c2", "content": plain},
        ]
        cleared = agent._clear_old_tool_results(keep_recent=1)
        self.assertEqual(cleared, 1)
        ph = agent.history[1]["content"]
        self.assertIn(ref["path"], ph)  # 指针还在：read_tool_result 可按需读回
        self.assertIn("read_tool_result", ph)
        # keep_recent=1 保留最近一条：无引用的结果原样保留（未被清理）
        self.assertEqual(agent.history[3]["content"], plain)


class TestStorageLifecycle(StorageTestBase):
    def test_write_dedupes_by_content(self):
        a = db.write_tool_result("s1", "same content")
        b = db.write_tool_result("s1", "same content")
        self.assertEqual(a["path"], b["path"])
        files = [f for f in (db._tool_results_dir() / "s1").iterdir()
                 if not f.name.endswith(".tmp")]
        self.assertEqual(len(files), 1)

    def test_delete_session_removes_files(self):
        ref = db.write_tool_result("s1", "to be deleted")
        self.assertTrue((db._tool_results_dir() / ref["path"]).exists())
        db.delete_session("s1")
        self.assertFalse((db._tool_results_dir() / ref["path"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
