"""
执行后端 seam（executor.py）的单元测试
（cd backend && python3 -m unittest tests.test_executor_seam -v）
================================================================

锁死 run_bash 与执行提供者之间的契约：工具层只管参数校验/信封组装/输出
截断拼合；提供者只管把命令跑起来（cwd 原样生效、timeout 由提供者强制、
超时以 timed_out=True 返回而非抛异常）。换 Docker 沙箱时这些测试原样适用
——注入 FakeExecutor 即可脱离本机子进程验证信封路径。

LocalExecutor 的真实子进程冒烟也在这里：它是默认提供者，行为必须与旧
run_bash 内联实现一致（这是本次接缝化的硬要求）。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from code_tools import BASH_TIMEOUT, run_bash
from executor import ExecResult, LocalExecutor
from tools import ToolContext


class FakeExecutor:
    """记录调用参数、返回预设结果的桩。"""

    def __init__(self, result: ExecResult):
        self.result = result
        self.calls = []

    def run(self, command, cwd, timeout):
        self.calls.append({"command": command, "cwd": str(cwd), "timeout": timeout})
        return self.result


class ExecutorSeamTest(unittest.TestCase):

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="exec_ws_"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.ctx = ToolContext(workspace=self.ws)
        self.fake = FakeExecutor(ExecResult(exit_code=0, stdout="hi\n", stderr=""))
        self.ctx.executor = self.fake

    def test_provider_receives_command_cwd_and_timeout(self):
        """接缝三要素：命令原文、工作区路径、工具层超时常量原样传入。

        cwd 用 resolve 后比较：macOS 的 /var ↔ /private/var 符号链接会让
        _ws 的 resolve 结果与 tempfile 返回的原路径字面不同，语义是同一个。"""
        run_bash("echo hi", self.ctx)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.calls[0]["command"], "echo hi")
        self.assertEqual(Path(self.fake.calls[0]["cwd"]).resolve(), self.ws.resolve())
        self.assertEqual(self.fake.calls[0]["timeout"], BASH_TIMEOUT)

    def test_success_envelope_from_provider_result(self):
        """成功：exit_code=0 → ok 信封，stdout 进 result（截断拼合是工具层职责）。"""
        self.fake.result = ExecResult(exit_code=0, stdout="out-line", stderr="")
        payload = json.loads(run_bash("anything", self.ctx))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["result"], "out-line")

    def test_stderr_appended_as_section(self):
        """stderr 独立捕获：以 [stderr] 段拼合（不与 stdout 混流）。"""
        self.fake.result = ExecResult(exit_code=0, stdout="o", stderr="e")
        payload = json.loads(run_bash("x", self.ctx))
        self.assertEqual(payload["result"], "o\n[stderr]\ne")

    def test_nonzero_exit_is_failure_envelope(self):
        """非零退出 = 命令失败（不是工具失败）：ok:false + 原始输出保留在 result。"""
        self.fake.result = ExecResult(exit_code=7, stdout="x", stderr="boom")
        payload = json.loads(run_bash("x", self.ctx))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 7)
        self.assertIn("7", payload["error"])
        self.assertIn("boom", payload["result"])

    def test_timeout_flag_maps_to_timeout_envelope(self):
        """提供者以 timed_out=True 报超时（不是抛异常）：信封给可执行的改道建议。"""
        self.fake.result = ExecResult(exit_code=-1, stdout="", stderr="", timed_out=True)
        payload = json.loads(run_bash("sleep 999", self.ctx))
        self.assertFalse(payload["ok"])
        self.assertIn("被终止", payload["error"])
        self.assertIn("分次执行", payload["hint"])

    def test_none_executor_falls_back_to_local(self):
        """ctx.executor 为 None（默认构造/CLI）：回落 LocalExecutor，真实执行。"""
        self.ctx.executor = None
        payload = json.loads(run_bash("echo seam-ok", self.ctx))
        self.assertTrue(payload["ok"])
        self.assertIn("seam-ok", payload["result"])

    def test_local_executor_real_process(self):
        """LocalExecutor 冒烟：真实子进程，行为与旧内联实现一致。"""
        res = LocalExecutor().run("printf 'a'; exit 3", cwd=self.ws, timeout=10)
        self.assertEqual(res.exit_code, 3)
        self.assertEqual(res.stdout, "a")
        res2 = LocalExecutor().run("echo ok", cwd=self.ws, timeout=10)
        self.assertEqual(res2.exit_code, 0)
        self.assertEqual(res2.stdout, "ok\n")

    def test_local_executor_timeout_returns_flag(self):
        """超时：ExecResult(timed_out=True)，绝不抛异常穿透到工具层。"""
        res = LocalExecutor().run("sleep 3", cwd=self.ws, timeout=0.4)
        self.assertTrue(res.timed_out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
