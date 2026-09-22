"""
文档存储层的单元测试（cd backend && python3 -m unittest tests.test_docs -v）
================================================================================

覆盖 docs 存储的核心不变式（全部跑在临时库里，不碰项目根）：
1. 写入/读取：写进 data/docs/<sid>/<name>.md，读回原文逐字节一致；同名覆盖；
2. 文件名归一：自动补 .md、去重复后缀、拒绝空名/带分隔符/..；
3. 路径校验：read_doc / _doc_path 拒绝 ../ 逃逸、跨会话、非 .md；
4. 越界防护：即便构造带 ../ 的 name 也逃不出 docs/<sid>/；
5. 列表：按 mtime 降序，只列 .md；
6. 大小上限：超 MAX_DOC_BYTES 拒绝；
7. 会话删除：docs/<sid>/ 整目录清理。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import db


class DocsTestBase(unittest.TestCase):
    """每个用例独占一个临时库：重定向 db.DB_PATH 后 init_db。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docs_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        db.create_session("s2", 1)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path


class TestWriteRead(DocsTestBase):

    def test_write_then_read_roundtrip(self):
        r = db.write_doc("s1", "汇报", "# 标题\n\n正文内容")
        self.assertEqual(r["name"], "汇报.md")
        self.assertEqual(db.read_doc("s1", "汇报.md"), "# 标题\n\n正文内容")
        # 落盘位置正确
        p = db._docs_dir() / "s1" / "汇报.md"
        self.assertTrue(p.is_file())

    def test_name_normalization(self):
        self.assertEqual(db.write_doc("s1", "a", "x")["name"], "a.md")
        self.assertEqual(db.write_doc("s1", "a.md", "x")["name"], "a.md")  # 不重复补
        self.assertEqual(db.write_doc("s1", "  b  ", "x")["name"], "b.md")  # 去空白

    def test_same_name_overwrites(self):
        db.write_doc("s1", "d", "第一版")
        db.write_doc("s1", "d", "第二版")
        self.assertEqual(db.read_doc("s1", "d.md"), "第二版")
        self.assertEqual(len(db.list_docs("s1")), 1)  # 同名不产生第二份

    def test_reject_bad_names(self):
        for bad in ["", "   ", "a/b", "a\\b", "..", "../x", "a/../b"]:
            with self.assertRaises(ValueError, msg=f"应拒绝: {bad!r}"):
                db.write_doc("s1", bad, "x")


class TestPathSafety(DocsTestBase):

    def test_read_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            db.read_doc("s1", "不存在.md")

    def test_cross_session_isolated(self):
        db.write_doc("s1", "only1", "属于s1")
        # s2 里没有这份文档
        with self.assertRaises(FileNotFoundError):
            db.read_doc("s2", "only1.md")
        self.assertEqual(db.list_docs("s2"), [])

    def test_escape_rejected(self):
        # 直接给 _doc_path 一个带 ../ 的 name（绕过 _safe_doc_name 归一）：
        # resolve 后逃出 docs/<sid>/ 必须被拒
        with self.assertRaises(ValueError):
            db._doc_path("s1", "../../evil.md")
        with self.assertRaises(ValueError):
            db._doc_path("s1", "../s2/x.md")

    def test_non_md_rejected(self):
        with self.assertRaises(ValueError):
            db._doc_path("s1", "note.txt")


class TestList(DocsTestBase):

    def test_list_only_md_and_sorted(self):
        import time
        db.write_doc("s1", "old", "a")
        time.sleep(0.01)
        db.write_doc("s1", "new", "b")
        names = [d["name"] for d in db.list_docs("s1")]
        self.assertEqual(names, ["new.md", "old.md"])  # 最近在前
        for d in db.list_docs("s1"):
            self.assertTrue(d["name"].endswith(".md"))
            self.assertIn("bytes", d)
            self.assertIn("mtime", d)

    def test_list_empty_for_unknown_session(self):
        self.assertEqual(db.list_docs("nope"), [])


class TestSizeLimit(DocsTestBase):

    def test_over_limit_rejected(self):
        big = "x" * (db.MAX_DOC_BYTES + 1)
        with self.assertRaises(ValueError):
            db.write_doc("s1", "big", big)
        # 未落盘
        self.assertFalse((db._docs_dir() / "s1" / "big.md").exists())


class TestSessionDelete(DocsTestBase):

    def test_delete_session_clears_docs(self):
        db.write_doc("s1", "d1", "内容")
        self.assertTrue((db._docs_dir() / "s1").is_dir())
        db.delete_session("s1")
        self.assertFalse((db._docs_dir() / "s1").exists())


class TestCreateDocTool(DocsTestBase):
    """create_doc 工具：走 ToolContext 注入的 session_id。"""

    def _ctx(self, sid):
        from tools import ToolContext
        return ToolContext(session_id=sid)

    def test_tool_writes_and_returns_ok(self):
        import json
        import doc_tools
        r = json.loads(doc_tools.create_doc("汇报", "# 标题\n正文", ctx=self._ctx("s1")))
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "汇报.md")
        self.assertEqual(db.read_doc("s1", "汇报.md"), "# 标题\n正文")

    def test_tool_without_session_fails_gracefully(self):
        import json
        import doc_tools
        r = json.loads(doc_tools.create_doc("x", "y", ctx=None))
        self.assertFalse(r["ok"])
        self.assertIn("会话", r["error"])

    def test_tool_rejects_bad_name(self):
        import json
        import doc_tools
        r = json.loads(doc_tools.create_doc("../evil", "y", ctx=self._ctx("s1")))
        self.assertFalse(r["ok"])

    def test_tool_registered_and_low_risk(self):
        import tools
        self.assertIn("create_doc", tools.TOOL_REGISTRY)
        # 非只读（写操作，串行），但在权限层属低危放行集合
        self.assertFalse(tools.is_read_only("create_doc"))
        import permissions
        self.assertIn("create_doc", permissions.READONLY_TOOLS)


if __name__ == "__main__":
    unittest.main()
