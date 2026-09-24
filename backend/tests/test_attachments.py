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

    def test_binary_returns_structured_kind(self):
        """二进制内容不再按 errors='replace' 吐乱码，而是返回 kind='binary'。

        行为变更（2026-09-23）：二进制/压缩包按文本解码只会得到一屏乱码，
        模型拿不到任何有用信息。改为返回结构化描述，让模型知道"这是什么、
        下一步怎么做"。文本附件仍是 kind='text'（见 test_write_read）。"""
        db.save_attachment("s1", "b.bin", bytes([0xff, 0xfe, 0x41]))
        got = db.read_attachment_text("s1", "b.bin")
        self.assertEqual(got["kind"], "binary")
        self.assertEqual(got["content"], "")


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

    def test_delete_attachment(self):
        """删除附件：文件消失、列表为空；再删一次幂等不报错。"""
        db.save_attachment("s1", "a.txt", b"x")
        db.delete_attachment("s1", "a.txt")
        self.assertEqual(db.list_attachments("s1"), [])
        db.delete_attachment("s1", "a.txt")  # 不存在视为已删，不抛

    def test_delete_attachment_cleans_extracted(self):
        """删压缩包附件时，_extracted/ 下的解压残留一并清掉。"""
        db.save_attachment("s1", "a.tar.gz", b"x")
        dest = db._session_attach_root("s1") / "_extracted" / "a.tar.gz"
        dest.mkdir(parents=True)
        (dest / "inner.txt").write_text("hi")
        db.delete_attachment("s1", "a.tar.gz")
        self.assertFalse(dest.exists())

    def test_delete_attachment_rejects_escape(self):
        """删除同样走 _attach_path 校验：../ 逃逸、跨会话都被拒。"""
        with self.assertRaises(ValueError):
            db.delete_attachment("s1", "../s2/a.txt")
        db.save_attachment("s2", "secret.txt", b"private")
        with self.assertRaises(ValueError):
            db.delete_attachment("s1", "../s2/secret.txt")
        self.assertTrue(db.list_attachments("s2"))  # s2 的附件毫发无损

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


class TestWorkspacePlacement(AttachTestBase):
    """附件落工作区 + 绑定工作区后迁移。

    为什么改：附件原先一律落 data/attachments/，在 agent 工作区之外——
    run_bash 的 cwd 锁在工作区、read_file 有越界校验，压缩包类附件因此
    完全没法用。改为落工作区后，附件是普通文件，一切文件能力自然可用。
    2026-09-29 起再深一层：工作区内也按 <sid> 分目录（.coding-agent/
    attachments/<sid>/），否则同一工作区的多个会话共用一份附件。
    """

    def _mk_ws(self, name="proj"):
        ws = Path(self.tmp) / name
        ws.mkdir()
        return ws

    def test_falls_back_to_data_without_workspace(self):
        """未绑定工作区时仍落 data/attachments，附件不丢。"""
        info = db.save_attachment("s1", "a.txt", b"x")
        self.assertIn("attachments", info["path"])
        self.assertNotIn("proj", info["path"])

    def test_lands_in_workspace_when_bound(self):
        """已绑定工作区：附件直接落工作区 .coding-agent/attachments/。"""
        ws = self._mk_ws()
        db.set_session_workspace("s1", str(ws))
        info = db.save_attachment("s1", "a.txt", b"x")
        self.assertTrue((ws / db.ATTACH_DIR_NAME / "s1" / "a.txt").exists())
        self.assertEqual(db.read_attachment_text("s1", "a.txt")["content"], "x")

    def test_sessions_in_same_workspace_are_isolated(self):
        """同一工作区绑定两个会话：附件互相不可见（按会话隔离）。"""
        ws = self._mk_ws()
        db.set_session_workspace("s1", str(ws))
        db.set_session_workspace("s2", str(ws))
        db.save_attachment("s1", "only-s1.txt", b"1")
        db.save_attachment("s2", "only-s2.txt", b"2")
        self.assertEqual([a["name"] for a in db.list_attachments("s1")], ["only-s1.txt"])
        self.assertEqual([a["name"] for a in db.list_attachments("s2")], ["only-s2.txt"])
        # s1 删除后，s2 的附件与目录结构不受影响
        db.delete_session("s1")
        self.assertFalse((ws / db.ATTACH_DIR_NAME / "s1").exists())
        self.assertTrue((ws / db.ATTACH_DIR_NAME / "s2" / "only-s2.txt").exists())
        self.assertEqual(db.read_attachment_text("s2", "only-s2.txt")["content"], "2")

    def test_migrate_moves_preexisting(self):
        """先上传（落 data/）后绑定工作区 → 迁移到工作区 <sid>/ 子目录。"""
        db.save_attachment("s1", "a.txt", b"hello")
        ws = self._mk_ws()
        db.set_session_workspace("s1", str(ws))
        moved = db.migrate_attachments_to_workspace("s1")
        self.assertEqual(moved, 1)
        self.assertTrue((ws / db.ATTACH_DIR_NAME / "s1" / "a.txt").exists())
        self.assertEqual(db.read_attachment_text("s1", "a.txt")["content"], "hello")

    def test_migrate_does_not_overwrite_workspace_copy(self):
        """工作区已有同名附件时不覆盖（保留用户后来上传的那份）。"""
        ws = self._mk_ws()
        db.set_session_workspace("s1", str(ws))
        db.save_attachment("s1", "a.txt", b"new-in-ws")
        # 手工在 data/ 放一份同名的旧文件，模拟"迁移前遗留"
        old = db._attachments_dir() / "s1"
        old.mkdir(parents=True, exist_ok=True)
        (old / "a.txt").write_bytes(b"stale")
        db.migrate_attachments_to_workspace("s1")
        self.assertEqual((ws / db.ATTACH_DIR_NAME / "s1" / "a.txt").read_bytes(), b"new-in-ws")

    def test_migrate_adopts_legacy_flat_files(self):
        """旧布局（.coding-agent/attachments/ 平铺无 <sid> 层）归给触发迁移的会话。"""
        ws = self._mk_ws()
        legacy = ws / db.ATTACH_DIR_NAME
        legacy.mkdir(parents=True)
        (legacy / "old.txt").write_bytes(b"legacy")
        db.set_session_workspace("s1", str(ws))
        moved = db.migrate_attachments_to_workspace("s1")
        self.assertEqual(moved, 1)
        self.assertTrue((legacy / "s1" / "old.txt").exists())
        self.assertFalse((legacy / "old.txt").exists())
        self.assertEqual(db.read_attachment_text("s1", "old.txt")["content"], "legacy")

    def test_delete_session_cleans_workspace_attach_dir(self):
        """删除会话时清掉工作区里本会话的附件子目录，但不动工作区其它内容。"""
        ws = self._mk_ws()
        db.set_session_workspace("s1", str(ws))
        db.save_attachment("s1", "a.txt", b"x")
        (ws / "keep.txt").write_text("用户自己的文件")
        db.delete_session("s1")
        self.assertFalse((ws / db.ATTACH_DIR_NAME).exists())
        self.assertTrue((ws / "keep.txt").exists())  # 工作区其它内容不受影响


if __name__ == "__main__":
    unittest.main()
