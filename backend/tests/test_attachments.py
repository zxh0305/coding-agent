"""
附件存储层的单元测试（cd backend && python3 -m unittest tests.test_attachments -v）
====================================================================================

覆盖 attachments 存储的核心不变式（全部跑在临时库里，不碰项目根）：
1. 写入/读取：写进 data/attachments/<sid>/<name>，读回原文逐字节一致；同名覆盖；
2. 分页：read_attachment_text 的 offset/limit 语义、truncated 标记；
3. 文件名归一：拒绝空名/带分隔符/..；保留原始扩展名（不同于 docs 强制 .md）；
4. 路径校验：拒绝 ../ 逃逸、跨会话读取；
5. 大小上限：超 MAX_ATTACH_BYTES 拒绝；超单会话总量拒绝；重传同名不误拒；
6. 列表：按 mtime 降序；
7. 会话删除：attachments/<sid>/ 整目录清理；
8. 孤儿清理：DB 里已无对应会话的目录被删除，存续会话的不动。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import db


class AttachTestBase(unittest.TestCase):
    """每个用例独占一个临时库：重定向 db.DB_PATH 后 init_db。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="attach_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        db.create_session("s2", 1)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path


class TestWriteRead(AttachTestBase):

    def test_write_read_roundtrip(self):
        """写进去的字节读回来逐字节一致。"""
        blob = "第一行\n第二行\n第三行\n".encode("utf-8")
        info = db.save_attachment("s1", "a.txt", blob)
        self.assertEqual(info["name"], "a.txt")
        self.assertEqual(info["bytes"], len(blob))
        got = db.read_attachment_text("s1", "a.txt")
        self.assertEqual(got["content"], "第一行\n第二行\n第三行")
        self.assertEqual(got["total_lines"], 3)

    def test_same_name_overwrites(self):
        """同名即覆盖（用户重传同名文件）。"""
        db.save_attachment("s1", "a.txt", b"old")
        db.save_attachment("s1", "a.txt", b"new")
        self.assertEqual(db.read_attachment_text("s1", "a.txt")["content"], "new")
        self.assertEqual(len(db.list_attachments("s1")), 1)

    def test_extension_preserved(self):
        """附件保留原始扩展名（与 docs 强制 .md 不同），模型据此判断类型。"""
        db.save_attachment("s1", "build.log", b"x")
        names = [a["name"] for a in db.list_attachments("s1")]
        self.assertIn("build.log", names)

    def test_binary_decoded_with_replace(self):
        """二进制内容不抛错，按 errors='replace' 退回文本。"""
        db.save_attachment("s1", "b.bin", bytes([0xff, 0xfe, 0x41]))
        got = db.read_attachment_text("s1", "b.bin")
        self.assertIn("A", got["content"])


class TestPaging(AttachTestBase):

    def _make_lines(self, n):
        return "\n".join(f"line{i}" for i in range(n)).encode("utf-8")

    def test_offset_limit(self):
        """分页语义：offset 起点、limit 条数、truncated 标记正确。"""
        db.save_attachment("s1", "big.txt", self._make_lines(10))
        page = db.read_attachment_text("s1", "big.txt", offset=2, limit=3)
        self.assertEqual(page["offset"], 2)
        self.assertEqual(page["lines"], 3)
        self.assertEqual(page["content"], "line2\nline3\nline4")
        self.assertEqual(page["total_lines"], 10)
        self.assertTrue(page["truncated"])

    def test_last_page_not_truncated(self):
        """读到文件尾时 truncated=False。"""
        db.save_attachment("s1", "big.txt", self._make_lines(5))
        page = db.read_attachment_text("s1", "big.txt", offset=0, limit=100)
        self.assertFalse(page["truncated"])
        self.assertEqual(page["lines"], 5)

    def test_offset_beyond_end(self):
        """offset 越界返回空内容，不抛错。"""
        db.save_attachment("s1", "big.txt", self._make_lines(3))
        page = db.read_attachment_text("s1", "big.txt", offset=99, limit=10)
        self.assertEqual(page["content"], "")
        self.assertEqual(page["lines"], 0)


class TestNameValidation(AttachTestBase):

    def test_reject_empty_and_dots(self):
        for bad in ("", "  ", ".", ".."):
            with self.assertRaises(ValueError):
                db.save_attachment("s1", bad, b"x")

    def test_reject_separators(self):
        for bad in ("a/b.txt", "a\\b.txt", "../x.txt"):
            with self.assertRaises(ValueError):
                db.save_attachment("s1", bad, b"x")


class TestPathEscape(AttachTestBase):

    def test_read_escape_rejected(self):
        """即便构造带 ../ 的 name 也逃不出 attachments/<sid>/。"""
        db.save_attachment("s1", "a.txt", b"x")
        with self.assertRaises(ValueError):
            db.read_attachment_text("s1", "../s2/a.txt")

    def test_cross_session_read_rejected(self):
        """s1 不能读 s2 的附件（跨会话隔离）。"""
        db.save_attachment("s2", "secret.txt", b"top secret")
        with self.assertRaises(ValueError):
            db.read_attachment_text("s1", "../s2/secret.txt")

    def test_missing_attachment(self):
        with self.assertRaises(FileNotFoundError):
            db.read_attachment_text("s1", "nope.txt")


class TestSizeLimits(AttachTestBase):

    def test_reject_oversize_file(self):
        """超单文件上限直接拒绝，不落盘。"""
        big = b"x" * (db.MAX_ATTACH_BYTES + 1)
        with self.assertRaises(ValueError):
            db.save_attachment("s1", "big.txt", big)
        self.assertEqual(db.list_attachments("s1"), [])

    def test_reject_over_session_total(self):
        """单会话总量超限时拒绝。"""
        chunk = b"x" * db.MAX_ATTACH_BYTES
        # 先塞满到接近上限（不触发单文件上限）
        n = db.MAX_ATTACH_TOTAL_BYTES // db.MAX_ATTACH_BYTES
        for i in range(n):
            db.save_attachment("s1", f"f{i}.txt", chunk)
        with self.assertRaises(ValueError):
            db.save_attachment("s1", "overflow.txt", chunk)

    def test_overwrite_same_name_not_falsely_rejected(self):
        """重传同名文件：总量按替换后计算，不会因旧文件计入而被误拒。"""
        chunk = b"x" * db.MAX_ATTACH_BYTES
        n = db.MAX_ATTACH_TOTAL_BYTES // db.MAX_ATTACH_BYTES
        for i in range(n):
            db.save_attachment("s1", f"f{i}.txt", chunk)
        # 覆盖已有文件：总量不变，应通过
        db.save_attachment("s1", "f0.txt", chunk)
        self.assertEqual(db.read_attachment_text("s1", "f0.txt")["content"][:1], "x")


class TestList(AttachTestBase):

    def test_list_sorted_by_mtime_desc(self):
        db.save_attachment("s1", "a.txt", b"a")
        db.save_attachment("s1", "b.txt", b"b")
        names = [x["name"] for x in db.list_attachments("s1")]
        self.assertEqual(names, ["b.txt", "a.txt"])

    def test_list_empty_session(self):
        self.assertEqual(db.list_attachments("s1"), [])


class TestDeleteAndCleanup(AttachTestBase):

    def test_delete_session_clears_attachments(self):
        """会话删除：attachments/<sid>/ 整目录清理。"""
        db.save_attachment("s1", "a.txt", b"x")
        self.assertTrue((db._attachments_dir() / "s1").exists())
        db.delete_session("s1")
        self.assertFalse((db._attachments_dir() / "s1").exists())

    def test_cleanup_orphans(self):
        """孤儿目录（DB 里已无对应会话）被清理，存续会话的附件不动。"""
        db.save_attachment("s1", "keep.txt", b"x")
        # 造一个孤儿目录：直接建目录，不建会话行
        orphan = db._attachments_dir() / "ghost"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "old.txt").write_bytes(b"z")
        removed = db.cleanup_orphan_attachments()
        self.assertEqual(removed, 1)
        self.assertFalse(orphan.exists())
        self.assertTrue((db._attachments_dir() / "s1").exists())

    def test_cleanup_noop_without_dir(self):
        """没有 attachments/ 目录时清理是空操作。"""
        self.assertEqual(db.cleanup_orphan_attachments(), 0)


if __name__ == "__main__":
    unittest.main()
