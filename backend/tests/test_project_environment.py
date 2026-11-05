import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from code_tools import run_bash
from services.project_environment import discover
from tests.test_session_model import ApiTestBase
from tools import ToolContext


class ProjectEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def environment(self, name):
        executable = self.root / name / "bin" / "python"
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
        return executable

    def test_multiple_candidates_require_selection_and_stale_choice_blocks(self):
        a, b = self.environment(".venv"), self.environment("venv")
        self.assertIsNone(discover(self.root)["selected"])
        profile = discover(self.root, str(a))
        self.assertEqual(profile["selected"]["path"], str(a))
        a.unlink()
        self.assertTrue(discover(self.root, str(a))["blocked"])
        self.assertFalse(discover(self.root, "system")["blocked"])

    def test_discovery_does_not_execute_and_configuration_is_bounded(self):
        exe = self.environment(".venv")
        config = self.root / ".agents" / "project.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"schema_version":1,
                                     "runtime":{"type":"python","executable":".venv/bin/python"}}))
        self.assertEqual(discover(self.root)["source"], "project")
        config.write_text("x" * 33000)
        self.assertTrue(discover(self.root)["diagnostics"])

    def test_shell_uses_selected_runtime_without_changing_global_environment(self):
        exe = self.environment(".venv")
        previous = os.environ.get("VIRTUAL_ENV")
        ctx = ToolContext(workspace=self.root, environment_path=str(exe))
        result = json.loads(run_bash("python -c 'import os; print(os.environ.get(\"VIRTUAL_ENV\"))'", ctx=ctx))
        self.assertTrue(result["ok"])
        self.assertIn(str(exe.parent.parent), result["result"])
        self.assertEqual(os.environ.get("VIRTUAL_ENV"), previous)


class ProjectContextApiTests(ApiTestBase):
    def test_context_owner_selection_and_rules(self):
        from urllib.parse import urlencode
        import db
        project = Path(self.tmp).resolve() / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("Use project tests")
        executable = project / ".venv" / "bin" / "python"
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
        db.set_session_workspace("s1", str(project))
        query = "/api/project-context?" + urlencode({"session_id":"s1"})
        code, context = self.call("GET", query)
        self.assertEqual(code, 200)
        self.assertEqual(context["rules"]["sources"][0]["content"], "Use project tests")
        self.assertEqual(self.call("POST", query, {"executable":"/other/python"})[0], 400)
        self.assertEqual(self.call("POST", query, {"executable":str(executable)})[0], 200)
        self.assertEqual(self.call("GET", "/api/project-context?session_id=unknown")[0], 404)
        self.assertEqual(self.call("GET", query, token=False)[0], 401)
