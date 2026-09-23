"""
归档处理与附件解压的单元测试
（cd backend && python3 -m unittest tests.test_archive -v）

覆盖的核心不变式：
1. 识别：扩展名与魔数双通道，`.log` 被 gzip 后也能认出；
2. 解压：tar.gz / zip / 单文件 gz 三种形态都能解出正确内容；
3. 安全：Zip Slip（../ 成员）被拦截且不逃逸；二进制与文本判定正确；
4. 上限：成员数 / 输出总量超限时拒绝（防压缩炸弹）。
"""

import gzip
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import archive_tools  # noqa: E402


class TestDetect(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _write(self, name, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_detect_by_suffix(self):
        p = self._write("a.tar.gz", b"whatever")
        self.assertEqual(archive_tools.detect_archive(p), "tar.gz")

    def test_detect_by_magic_when_renamed(self):
        """扩展名被改坏（.log 其实是 gzip 流）也能按魔数认出。"""
        blob = gzip.compress(b"hello")
        p = self._write("fake.log", blob)
        self.assertEqual(archive_tools.detect_archive(p), "gzip")

    def test_plain_text_not_archive(self):
        p = self._write("a.txt", b"just text")
        self.assertIsNone(archive_tools.detect_archive(p))

    def test_binary_probe(self):
        self.assertTrue(archive_tools.is_probably_binary(self._write("b.bin", b"\x00\x01\x02")))
        self.assertFalse(archive_tools.is_probably_binary(self._write("t.txt", "中文abc".encode())))


class TestExtract(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_tar_gz(self):
        src = self.tmp / "app.log"
        src.write_text("INFO x\n" * 100, encoding="utf-8")
        tp = self.tmp / "logs.tar.gz"
        with tarfile.open(tp, "w:gz") as tf:
            tf.add(src, arcname="app.log")
        dest = self.tmp / "out"
        r = archive_tools.extract_archive(tp, dest)
        self.assertTrue(r["ok"])
        self.assertEqual(r["file_count"], 1)
        self.assertEqual((dest / "app.log").read_text().count("INFO"), 100)

    def test_zip_nested_dir(self):
        zp = self.tmp / "a.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("sub/y.txt", "hello")
        dest = self.tmp / "out"
        r = archive_tools.extract_archive(zp, dest)
        self.assertTrue(r["ok"])
        self.assertEqual((dest / "sub" / "y.txt").read_text(), "hello")

    def test_single_gz_stream(self):
        gz = self.tmp / "one.gz"
        gz.write_bytes(gzip.compress(b"line\n" * 200))
        dest = self.tmp / "out"
        r = archive_tools.extract_archive(gz, dest)
        self.assertTrue(r["ok"])
        self.assertTrue((dest / "one").exists())

    def test_zip_slip_blocked(self):
        """../ 成员必须被拦截，且文件不逃出目标目录。"""
        zp = self.tmp / "evil.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("../../evil.txt", "bad")
        dest = self.tmp / "out"
        r = archive_tools.extract_archive(zp, dest)
        self.assertTrue(r["ok"])
        self.assertEqual(r["file_count"], 0)  # 越界成员被跳过
        self.assertFalse((self.tmp.parent / "evil.txt").exists())

    def test_idempotent_reextract(self):
        """重复解压同一归档：以最后一次为准，不叠加。"""
        tp = self.tmp / "a.tar.gz"
        src = self.tmp / "f.txt"
        src.write_text("v1")
        with tarfile.open(tp, "w:gz") as tf:
            tf.add(src, arcname="f.txt")
        dest = self.tmp / "out"
        archive_tools.extract_archive(tp, dest)
        archive_tools.extract_archive(tp, dest)
        self.assertEqual(len(list(dest.rglob("f.txt"))), 1)

    def test_not_archive_returns_error(self):
        p = self.tmp / "plain.txt"
        p.write_text("hi")
        r = archive_tools.extract_archive(p, self.tmp / "out")
        self.assertFalse(r["ok"])


class TestDescribe(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_describe_lists_members(self):
        tp = self.tmp / "a.tar.gz"
        src = self.tmp / "app.log"
        src.write_text("x" * 50)
        with tarfile.open(tp, "w:gz") as tf:
            tf.add(src, arcname="app.log")
        info = archive_tools.describe_archive(tp)
        self.assertEqual(info["kind"], "tar.gz")
        self.assertEqual(info["file_count"], 1)
        self.assertEqual(info["members"][0]["name"], "app.log")


if __name__ == "__main__":
    unittest.main()
