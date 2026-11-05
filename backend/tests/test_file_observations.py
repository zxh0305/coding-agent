import json
import tempfile
import unittest
from pathlib import Path

from agent import Agent
from code_tools import read_file, write_file, apply_patch


class FileObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "main.py"
        self.file.write_text("old = 1\nkeep = 2\n")
        self.agent = Agent(llm=None, workspace=self.root)
        self.ctx = self.agent.ctx

    def test_unread_and_external_change_refuse_patch(self):
        first = json.loads(apply_patch("main.py", "old = 1", "old = 3", ctx=self.ctx))
        self.assertEqual(first["error_code"], "FS_NOT_OBSERVED")
        read_file("main.py", ctx=self.ctx)
        self.file.write_text("old = 1\nuser_edit = 9\n")
        conflict = json.loads(apply_patch("main.py", "old = 1", "old = 3", ctx=self.ctx))
        self.assertEqual(conflict["error_code"], "FS_STALE_VERSION")
        self.assertIn("user_edit", self.file.read_text())

    def test_page_reads_allow_patch_but_require_all_pages_for_overwrite(self):
        read_file("main.py", limit=1, ctx=self.ctx)
        result = json.loads(write_file("main.py", "new = 1", ctx=self.ctx))
        self.assertEqual(result["error_code"], "FS_NOT_FULLY_READ")
        read_file("main.py", offset=2, ctx=self.ctx)
        self.assertTrue(json.loads(write_file("main.py", "new = 1", ctx=self.ctx))["ok"])
        self.assertTrue(json.loads(apply_patch("main.py", "new = 1", "new = 2", ctx=self.ctx))["ok"])

    def test_sessions_do_not_share_read_proof(self):
        read_file("main.py", ctx=self.ctx)
        other = Agent(llm=None, workspace=self.root)
        self.assertEqual(json.loads(write_file("main.py", "overwrite", ctx=other.ctx))["error_code"],
                         "FS_NOT_OBSERVED")

    def test_new_file_and_empty_file_work(self):
        self.assertTrue(json.loads(write_file("new.py", "", ctx=self.ctx))["ok"])
        self.assertTrue(json.loads(write_file("new.py", "print(1)", ctx=self.ctx))["ok"])
        self.file.write_text("")
        self.assertTrue(json.loads(read_file("main.py", ctx=self.ctx))["ok"])
        self.assertTrue(json.loads(write_file("main.py", "text", ctx=self.ctx))["ok"])


    def test_invalid_encoding_cannot_be_silently_rewritten(self):
        self.file.write_bytes(b"header=1\ntext=\xff\n")
        read_file("main.py", ctx=self.ctx)
        result = json.loads(apply_patch("main.py", "header=1", "header=2", ctx=self.ctx))
        self.assertEqual(result["error_code"], "FS_ENCODING")
        self.assertIn(b"\xff", self.file.read_bytes())
