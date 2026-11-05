import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import Agent
from services.project_context import ProjectContext, MAX_CONTEXT_BYTES


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_root_nested_and_updates_are_model_visible(self):
        (self.root / "AGENTS.md").write_text("Use project tests", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("Use project tests", encoding="utf-8")
        nested = self.root / "src"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("Keep route compatibility", encoding="utf-8")
        context = ProjectContext(self.root)
        self.assertEqual(len(context.refresh()["sources"]), 1)
        context.touch("src/api.py")
        self.assertEqual(len(context.refresh()["sources"]), 2)
        (nested / "AGENTS.md").unlink()
        self.assertNotIn("Keep route", context.refresh()["content"])
        context.touch("../sibling/api.py")
        self.assertEqual(len(context.refresh()["sources"]), 1)

    def test_budget_and_symlink_diagnostics(self):
        (self.root / "AGENTS.md").write_text("界" * MAX_CONTEXT_BYTES, encoding="utf-8")
        (self.root / "CLAUDE.md").symlink_to(self.root / "AGENTS.md")
        context = ProjectContext(self.root).refresh()
        self.assertLessEqual(len(context["content"].encode()), MAX_CONTEXT_BYTES)
        self.assertTrue(context["diagnostics"])

    def test_agent_uses_current_rules_without_putting_them_in_history(self):
        file = self.root / "AGENTS.md"
        file.write_text("Never edit production", encoding="utf-8")
        agent = Agent(llm=None, workspace=self.root)
        with patch.object(agent, "_memory_blocks", return_value=""):
            self.assertIn("Never edit production", agent._system_content())
            file.write_text("Run local tests", encoding="utf-8")
            self.assertIn("Run local tests", agent._system_content())
            self.assertNotIn("Never edit production", agent._system_content())
            self.assertEqual(agent.history, [])

    def test_web_turn_records_the_rules_used_by_the_model(self):
        import app
        import db
        from services import turn
        from unittest.mock import Mock
        with patch.object(db, "DB_PATH", self.root / "isolated.db"):
            db.init_db()
            db.create_session("rules", 1)
            (self.root / "AGENTS.md").write_text("Run configured tests")
            agent = Agent(llm=Mock(), workspace=self.root)
            def run(plain, message):
                agent.history.append(message)
                self.assertIn("Run configured tests", agent._system_content())
                agent.history.append({"role":"assistant", "content":"answer"})
                yield "done", {"answer":"answer"}
            agent.run = run
            with patch.object(turn, "_spawn_title_generation"), patch.object(turn, "_spawn_memory_extraction"):
                app._run_round("rules", agent, "question", {"role":"user", "content":"question"}, "n", [])
            message = next(m for m in db.get_messages("rules") if m["role"] == "user")
            reference = message["_project_context"][0]["path"]
            context = json.loads("\n".join(db.read_tool_result(reference)["lines"]))
            self.assertEqual(context["sources"][0]["content"], "Run configured tests")
