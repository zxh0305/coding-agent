"""停止协议回归测试：长命令能被"停止"真正掐断。

修前行为：run_bash 把命令整个交给 executor.run(timeout=...)，**命令跑完才**
回到 agent 的循环、才检查 cancel_event——一条 30 秒的 npm install 意味着
"停止"实际要等 30 秒，用户观感是点了没反应。

修后：
  1. Executor.run 接受 cancel，分片轮询，置位后立刻杀进程组；
  2. 返回 cancelled=True（与自然超时区分：超时是命令的错，取消是用户的意图）；
  3. run_bash 把 cancelled 翻成明确的工具结果，让模型"别重试"；
  4. 旧提供者（不接受 cancel 形参）行为不变。

（cd backend && python3 -m unittest tests.test_cancel -v）
"""

import threading
import time
import unittest

import code_tools
from executor import ExecResult, LocalExecutor


class CancelExecutorTest(unittest.TestCase):

    def test_cancel_kills_long_command_quickly(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        res = LocalExecutor().run("sleep 30", cwd=".", timeout=60, cancel=cancel)
        elapsed = time.monotonic() - t0
        self.assertTrue(res.cancelled)
        self.assertFalse(res.timed_out, "取消不是超时：错误归因必须区分")
        self.assertLess(elapsed, 3.0, f"应当在取消后立刻返回，实际 {elapsed:.1f}s")

    def test_timeout_still_works_without_cancel(self):
        res = LocalExecutor().run("sleep 5", cwd=".", timeout=1)
        self.assertTrue(res.timed_out)
        self.assertFalse(res.cancelled)

    def test_cancel_event_unset_runs_to_completion(self):
        cancel = threading.Event()
        res = LocalExecutor().run("echo hi", cwd=".", timeout=10, cancel=cancel)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("hi", res.stdout)
        self.assertFalse(res.cancelled)

    def test_cancel_keeps_partial_output(self):
        """取消时也要把已经产生的输出带回来（模型能看到停止前跑出了什么）。"""
        cancel = threading.Event()
        t = threading.Timer(0.6, cancel.set)
        t.start()
        res = LocalExecutor().run("echo before-cancel; sleep 30", cwd=".",
                                  timeout=60, cancel=cancel)
        t.join()
        self.assertTrue(res.cancelled)
        self.assertIn("before-cancel", res.stdout)

    def test_cancel_kills_grandchildren(self):
        """复合命令的孙进程也要死：否则被"停止"掐断的测试/构建会在后台继续跑，
        长会话里累积占 CPU 与端口（executor 模块头注释记录的原始事故）。"""
        import subprocess
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        res = LocalExecutor().run(
            "sh -c 'sleep 30 & wait'", cwd=".", timeout=60, cancel=cancel)
        self.assertTrue(res.cancelled)
        time.sleep(0.5)
        out = subprocess.run(["ps", "-eo", "command"], capture_output=True, text=True).stdout
        leftovers = [ln for ln in out.splitlines() if "sleep 30" in ln]
        self.assertEqual(leftovers, [], f"孙进程未被清理：{leftovers}")


class FakeCtx:
    def __init__(self, workspace, cancel=None, executor=None):
        self.workspace = workspace
        self.cancel_event = cancel
        self.executor = executor


class RunBashCancelTest(unittest.TestCase):

    def test_run_bash_reports_cancel(self):
        env = type("E", (), {"run": lambda self, c, cwd, timeout, cancel=None:
                             ExecResult(-1, "partial", "", cancelled=True)})()
        out = code_tools.run_bash("make test", ctx=FakeCtx("/tmp", threading.Event(), env))
        self.assertIn("被用户停止", out)
        self.assertIn("不要重试", out)

    def test_run_bash_passes_cancel_to_executor(self):
        seen = {}

        class E:
            def run(self, command, cwd, timeout, cancel=None):
                seen["cancel"] = cancel
                return ExecResult(0, "ok", "")

        ev = threading.Event()
        code_tools.run_bash("echo ok", ctx=FakeCtx("/tmp", ev, E()))
        self.assertIs(seen.get("cancel"), ev, "cancel 必须透传给执行器")

    def test_legacy_executor_without_cancel_still_works(self):
        """不接受 cancel 形参的旧提供者（含 *args 签名）不能被探针搞坏。"""
        class Legacy:
            def run(self, command, cwd, timeout):
                return ExecResult(0, "legacy ok", "")

        out = code_tools.run_bash("echo ok", ctx=FakeCtx("/tmp", threading.Event(), Legacy()))
        self.assertIn("legacy ok", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
