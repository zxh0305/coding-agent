import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import Agent
from services.task_progress import ProgressWindow, task_state
from services.tool_feedback import annotate
from tests.test_wrapup import RecordingLLM, call, tool_call_message, asst
from tests.test_toolresult_ref import StorageTestBase


class FeedbackTest(unittest.TestCase):
    def result(self, name="run_bash", command="make test", **info):
        return json.loads(annotate(name, {"command":command}, json.dumps(info)))

    def test_statuses_keep_legacy_evidence(self):
        for extra, status in [({"cancelled":True}, "cancelled"), ({"timed_out":True}, "timeout"),
                              ({"error_code":"PERMISSION_DENIED"}, "denied"),
                              ({"error_code":"EXECUTOR_ERROR"}, "executor_error"),
                              ({"exit_code":2}, "command_exit"), ({}, "error")]:
            with self.subTest(status=status):
                value = self.result(ok=False, error="evidence", **extra)
                self.assertFalse(value["ok"])
                self.assertEqual(value["error"], "evidence")
                self.assertEqual(value["outcome"], status)

    def test_simple_queries_are_not_tool_faults(self):
        for command, status in [("rg missing .", "no_match"), ("git diff --exit-code", "differences"),
                                ("rg missing . | cat", "command_exit"),
                                ("rg missing .; false", "command_exit"),
                                ("python test.py", "command_exit")]:
            with self.subTest(command=command):
                value = self.result(command=command, ok=False, exit_code=1, result="original")
                self.assertEqual(value["outcome"], status)
                self.assertEqual(value["result"], "original")
                self.assertEqual(value["exit_code"], 1)

    def test_unstructured_results_and_success_remain_compatible(self):
        for original in ("plain text", "[]", '{"result":"custom"}'):
            self.assertEqual(annotate("custom", {}, original), original)
        self.assertEqual(self.result(name="grep", ok=True, total=0)["outcome"], "no_match")

    def test_missing_file_and_executor_fault_have_distinct_codes(self):
        from code_tools import read_file, run_bash
        from tools import ToolContext
        with tempfile.TemporaryDirectory() as folder:
            context = ToolContext(workspace=folder)
            self.assertEqual(json.loads(read_file("missing", ctx=context))["error_code"], "PATH_NOT_FOUND")
            class Broken:
                def run(self, command, cwd, timeout):
                    raise RuntimeError("offline")
            context.executor = Broken()
            result = json.loads(run_bash("anything", ctx=context))
            self.assertEqual(result["error_code"], "EXECUTOR_ERROR")
            self.assertNotIn("exit_code", result)

    def test_missing_dependency_keeps_command_exit_and_environment_hint(self):
        from code_tools import run_bash
        from tools import ToolContext
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            class MissingModule:
                def run(self, command, cwd, timeout):
                    return SimpleNamespace(exit_code=1, timed_out=False, stdout="",
                                           stderr="ModuleNotFoundError: No module named 'sample'\n")
            context = ToolContext(workspace=folder, executor=MissingModule())
            result = json.loads(run_bash("python test.py", ctx=context))
            self.assertEqual(result["error_code"], "DEPENDENCY_MISSING")
            self.assertEqual(result["exit_code"], 1)
            self.assertIn("项目环境", result["hint"])


class ProgressTest(unittest.TestCase):
    def test_alternating_loop_warns_once(self):
        window = ProgressWindow()
        for i in range(6):
            window.observe("read_file", {"path":str(i % 2)}, {"ok":True, "version":"same"})
        self.assertEqual(window.pending, ["read_file"])
        window.pending = None
        for i in range(6):
            window.observe("read_file", {"path":str(i % 2)}, {"ok":True, "version":"same"})
        self.assertIsNone(window.pending)

    def test_new_evidence_and_waiting_do_not_warn(self):
        for mode in ("version", "new_path", "waiting"):
            with self.subTest(mode=mode):
                window = ProgressWindow()
                for i in range(12):
                    name = "wait_process" if mode == "waiting" and i % 3 == 0 else "read_file"
                    window.observe(name, {"path":str(i if mode == "new_path" else i % 2)},
                                   {"ok":True, "version":str(i if mode == "version" else "same")})
                self.assertIsNone(window.pending)

    def test_state_preserves_host_facts_across_repeated_compaction(self):
        first = task_state([
            {"role":"user", "content":"修复登录"},
            {"role":"tool", "_task_evidence":{"modified":"login.py", "pending":["运行测试"]}},
            {"role":"tool", "_task_evidence":{"check":{"exit_code":1}}},
        ])
        second = task_state([
            {"role":"user", "content":"原始请求"},
            {"role":"user", "content":"模型摘要", "_synthetic":True, "_task_state":first},
            {"role":"user", "content":"再修复退出登录"},
            {"role":"tool", "_task_evidence":{"modified":"logout.py"}},
        ])
        self.assertEqual(second["latest_request"], "再修复退出登录")
        self.assertEqual(second["modified_files"], ["login.py", "logout.py"])
        self.assertEqual(second["pending"], ["运行测试"])
        self.assertEqual(second["checks"], [{"exit_code":1}])


class AgentProgressTest(unittest.TestCase):
    def make_agent(self, script, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Agent(llm=RecordingLLM(script), workspace=directory.name, verbose=False, **options)

    def test_alternating_loop_is_advisory_and_one_final_answer(self):
        script = [tool_call_message(call(str(i), "read_file", path=str(i % 2))) for i in range(6)] + [asst("明确卡点")]
        agent = self.make_agent(script, max_rounds=30)
        events = list(agent.run("调查问题"))
        reminders = [item for item in agent.trace if item.get("kind") == "no_progress"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(sum(kind == "done" for kind, _ in events), 1)
        self.assertEqual(events[-1][1]["answer"], "明确卡点")

    def test_todo_and_shell_zero_do_not_extend_rounds(self):
        for name, args in [("todo_write", {"todos":[{"content":"看看", "status":"pending"}]}),
                           ("run_bash", {"command":"true"})]:
            with self.subTest(name=name):
                agent = self.make_agent([tool_call_message(call("one", name, **args)), asst("收尾")], max_rounds=1)
                events = list(agent.run("工作"))
                self.assertTrue(any(p.get("wrap_up") for k, p in events if k == "round"))

    def test_identical_write_is_not_progress(self):
        agent = self.make_agent([], max_rounds=1)
        target = Path(agent.ctx.workspace) / "same.txt"
        target.write_text("same")
        agent.llm.script = [tool_call_message(call("read", "read_file", path="same.txt")),
                            tool_call_message(call("write", "write_file", path="same.txt", content="same")), asst("结束")]
        agent.max_rounds = 2
        events = list(agent.run("检查"))
        self.assertTrue(any(p.get("wrap_up") for k, p in events if k == "round"))

    def test_shell_changes_with_policy_are_progress(self):
        from services.verification import VerificationPolicy
        agent = self.make_agent([])
        policy = VerificationPolicy(agent.ctx.workspace)
        policy.start(agent)
        agent.completion_policy = policy
        agent.environment_profile = {}
        request = call("shell", "run_bash", command="create")
        def execute(name, args, ctx):
            (Path(ctx.workspace) / "new.txt").write_text("changed")
            return json.dumps({"ok":True, "exit_code":0, "result":"done"})
        with patch("agent.execute_tool", execute):
            agent._run_tool(request)
        self.assertTrue(request.get("_made_progress"))

    def test_compact_state_reenters_model_without_internal_fields(self):
        agent = self.make_agent([])
        state = {"latest_request":"继续", "modified_files":["actual.py"], "checks":[], "pending":["验证"]}
        agent.history = [{"role":"user", "content":"修复"},
                         {"role":"compact", "content":"模型摘要", "_task_state":state},
                         {"role":"assistant", "content":"继续处理"}]
        messages = agent._messages_for_model()
        self.assertIn("actual.py", messages[1]["content"])
        self.assertIn("不等于检查通过", messages[1]["content"])
        self.assertNotIn("_task_state", messages[1])

    def test_changed_evidence_breaks_consecutive_repeat_streak(self):
        agent = self.make_agent([])
        agent._streak_sig, agent._streak_count = None, 0
        agent._reminders_used = 0
        agent._budget_remind_rounds = set()
        agent._denied_sigs = set()
        for i in range(6):
            request = call(str(i), "read_file", path="same")
            agent._backfill_tool_result(request, json.dumps({"ok":True, "version":str(i)}))
            agent._maybe_remind([request], i)
        self.assertEqual(agent._reminders_used, 0)

    def test_internal_call_fields_never_reach_provider(self):
        agent = self.make_agent([tool_call_message(call("one", "write_file", path="new.txt", content="new")), asst("完成")])
        list(agent.run("创建文件"))
        request = agent.llm.requests[-1]["messages"]
        calls = [c for message in request for c in message.get("tool_calls", [])]
        self.assertTrue(calls)
        self.assertFalse(any(k.startswith("_") for c in calls for k in c))

    def test_model_call_id_cannot_claim_host_check_evidence(self):
        agent = self.make_agent([])
        agent._backfill_tool_result(call("check_invented", "run_bash", command="true"),
                                    json.dumps({"ok":True, "exit_code":0, "result":"done"}))
        self.assertNotIn("_task_evidence", agent.history[-1])

    def test_optional_progress_inspection_failure_does_not_block_tools(self):
        from types import SimpleNamespace
        agent = self.make_agent([])
        def failed_state(agent):
            raise RuntimeError("optional observer unavailable")
        agent.completion_policy = SimpleNamespace(_state=failed_state)
        request = call("shell", "run_bash", command="true")
        with patch("agent.execute_tool", return_value=json.dumps({"ok":True, "exit_code":0})) as execute:
            self.assertTrue(json.loads(agent._run_tool(request))["ok"])
        execute.assert_called_once()


class EvidenceStorageTest(StorageTestBase):
    def test_evidence_survives_archive_reload_and_idempotent_save(self):
        import db
        evidence = {"modified":"actual.py"}
        history = [{"role":"user", "content":"任务"},
                   {"role":"tool", "content":"large " * 20000, "tool_call_id":"one", "_task_evidence":evidence}]
        saved = {}
        db.save_messages("s1", history, saved)
        self.assertTrue(history[1].get("_artifact"))
        restored = db.restore_window("s1")
        self.assertEqual(restored[1]["_task_evidence"], evidence)
        self.assertEqual(task_state(restored)["modified_files"], ["actual.py"])
        self.assertEqual(db.save_messages("s1", restored, db.fingerprints(restored)), 0)
