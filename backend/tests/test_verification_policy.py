import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent import Agent
from permissions import PermissionGate
from services.verification import VerificationPolicy
from tests.test_wrapup import RecordingLLM, tool_call_message, call, asst


class VerificationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".agents").mkdir()
        (self.root / ".agents" / "project.json").write_text(json.dumps({
            "schema_version":1, "checks":[{"id":"unit", "argv":[sys.executable, "check.py"],
                                           "level":"unit"}]}))
        (self.root / "check.py").write_text(
            "from pathlib import Path\nassert Path('value.txt').read_text() == 'good'\n")

    def agent(self, script, **kwargs):
        gate = PermissionGate(self.root, ask_timeout=0, mode_loader=lambda:"yolo")
        agent = Agent(llm=RecordingLLM(script), workspace=self.root, permission_gate=gate, verbose=False, **kwargs)
        policy = VerificationPolicy(self.root)
        policy.start(agent)
        agent.completion_policy = policy
        return agent, policy

    def test_failed_check_is_fixed_before_one_final_done(self):
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad")),
            asst("candidate"),
            tool_call_message(call("fix", "apply_patch", path="value.txt", search="bad", replace="good")),
            asst("fixed candidate"),
            asst("final report"),
        ])
        events = list(agent.run("fix"))
        self.assertEqual(policy.assessment, "passed")
        self.assertEqual(policy.repairs, 1)
        self.assertEqual(len([e for e in events if e[0] == "done"]), 1)
        self.assertIn("自动检查通过", events[-1][1]["answer"])
        self.assertIsNone(agent.llm.requests[-1]["tools"])
        # The exact original check is run again, rather than a weaker replacement.
        results = [p for k,p in events if k == "check_update" and p["status"] != "running"]
        self.assertEqual([r["status"] for r in results], ["failed", "passed"])
        self.assertEqual(results[0]["cmd"], results[1]["cmd"])

    def test_unmodified_question_never_runs_checks(self):
        agent, policy = self.agent([asst("hello")])
        events = list(agent.run("hi"))
        self.assertEqual(policy.assessment, "not_applicable")
        self.assertFalse(any(k == "check_update" for k,p in events))

    def test_permission_denial_blocks_and_does_not_retry(self):
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad")),
            asst("candidate"), asst("blocked report")
        ])
        agent.permissions = PermissionGate(self.root, ask_timeout=0, user_rules_loader=lambda:[
            {"tool":"run_bash", "decision":"deny"}])
        events = list(agent.run("fix"))
        self.assertEqual(policy.assessment, "blocked")
        self.assertEqual(policy.repairs, 0)
        self.assertIn("验证受阻", events[-1][1]["answer"])

    def test_unchanged_failed_candidate_finishes_with_evidence(self):
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad")),
            asst("candidate"), asst("no fix"), asst("final")
        ])
        events = list(agent.run("fix"))
        self.assertEqual(policy.assessment, "failed")
        self.assertEqual(policy.batches, 1)
        self.assertIn("仍有失败", events[-1][1]["answer"])

    def test_stop_before_dispatch_cancels_check_and_no_repair(self):
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad")),
            asst("candidate")
        ])
        gen = agent.run("fix")
        events = []
        for kind, payload in gen:
            events.append((kind, payload))
            if kind == "check_update" and payload["status"] == "running":
                agent.stop()
                break
        events.extend(gen)
        self.assertEqual(policy.assessment, "cancelled")
        self.assertEqual(policy.repairs, 0)
        self.assertEqual(len([p for k,p in events if k == "done"]), 1)
        self.assertTrue(next(p for k,p in events if k == "done")["stopped"])
        self.assertFalse(any(p.get("status") == "passed" for k,p in events if k == "check_update"))

    def test_check_that_changes_inputs_cannot_claim_pass(self):
        (self.root / "check.py").write_text(
            "from pathlib import Path\nPath('value.txt').write_text('changed externally')\n")
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad")),
            asst("candidate"), asst("final")
        ])
        events = list(agent.run("fix"))
        self.assertEqual(policy.assessment, "insufficient")
        self.assertEqual(policy.results[0]["status"], "stale")
        self.assertIn("已过期", events[-1][1]["answer"])

    def test_repair_budget_is_bounded(self):
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad0")),
            asst("candidate0"),
            tool_call_message(call("fix1", "apply_patch", path="value.txt", search="bad0", replace="bad1")),
            asst("candidate1"),
            tool_call_message(call("fix2", "apply_patch", path="value.txt", search="bad1", replace="bad2")),
            asst("candidate2"), asst("final")
        ])
        events = list(agent.run("fix"))
        self.assertEqual(policy.repairs, 2)
        self.assertEqual(policy.batches, 3)
        self.assertEqual(policy.assessment, "failed")
        self.assertEqual(len([p for k,p in events if k == "done"]), 1)

    def test_existing_untracked_files_do_not_trigger_checks_on_question(self):
        import snapshot
        from tests.test_snapshot_verify import git
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@local")
        git(self.root, "config", "user.name", "Test")
        (self.root / "tracked").write_text("initial")
        git(self.root, "add", "tracked")
        git(self.root, "commit", "-qm", "initial")
        agent, _ = self.agent([asst("answer")])
        policy = VerificationPolicy(self.root, snapshot.create(self.root))
        policy.start(agent)
        agent.completion_policy = policy
        events = list(agent.run("explain"))
        self.assertFalse(any(k == "check_update" for k,p in events))

    def test_final_only_response_cannot_start_new_tool(self):
        agent, policy = self.agent([
            tool_call_message(call("write", "write_file", path="value.txt", content="bad")),
            asst("candidate"), asst("no fix"),
            tool_call_message(call("forbidden", "write_file", path="unexpected.txt", content="x")),
            asst("只报告实际结果")
        ])
        events = list(agent.run("fix"))
        self.assertFalse((self.root / "unexpected.txt").exists())
        self.assertEqual(len([p for k,p in events if k == "done"]), 1)
