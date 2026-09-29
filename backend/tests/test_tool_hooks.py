"""
工具执行钩子（pre 否决 / post 改写）的单元测试
（cd backend && python3 -m unittest tests.test_tool_hooks -v）
==============================================================

借鉴 dsh 的 pre/execute/post 三段事件，简化为两段可插拔钩子：扩展面
（审计/沙箱策略/子代理权限继承）从此不必再动主循环。锁死四条契约：
1. pre 返回非空字符串 = 否决：工具不执行、信封 ok:false、post 不运行；
2. pre/post 抛异常一律忽略（钩子绝不能拖垮主循环——_run_tool 的
   "绝不抛"承诺覆盖钩子）；
3. post 链式改写：返回新串生效、返回 None 保持原样；
4. 内置 recent_reads 记录钩子：成功读取入列、失败不入、超容量裁剪。

权限 ask 闸门【刻意不】迁移到钩子：ask 的等待语义必须活在回合线程
（_execute_tool_calls 阶段〇），这里只测无需等待的同步扩展面。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import Agent, RECENT_READS_KEEP


class _NullLLM:
    pass  # 钩子测试只走 _run_tool，不发任何请求


class ToolHooksTest(unittest.TestCase):

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="hooks_ws_"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        (self.ws / "a.txt").write_text("hello", encoding="utf-8")
        self.agent = Agent(llm=_NullLLM(), verbose=False, workspace=str(self.ws))

    def run_tool(self, name, arguments):
        return json.loads(self.agent._run_tool(
            {"function": {"name": name, "arguments": json.dumps(arguments)}}))

    # ---- pre 钩子：否决 ------------------------------------------------

    def test_pre_hook_veto_blocks_execution(self):
        """pre 返回拒绝原因：工具不执行（副作用零发生）、信封 ok:false、post 不跑。"""
        post_seen = []
        self.agent.pre_tool_hooks.append(
            lambda name, raw, ctx: "演示禁止" if name == "read_file" else None)
        self.agent.post_tool_hooks.append(
            lambda name, args, result: post_seen.append(name) or None)
        r = self.run_tool("read_file", {"path": "a.txt"})
        self.assertFalse(r["ok"])
        self.assertIn("被拦截", r["error"])
        self.assertIn("演示禁止", r["error"])
        self.assertNotIn("result", r)          # 绝不能带着执行结果
        self.assertEqual(post_seen, [])        # 否决的调用不进 post
        # 其它工具不受影响
        self.assertTrue(self.run_tool("list_dir", {"path": "."})["ok"])

    def test_pre_hook_exception_is_ignored(self):
        """钩子抛异常 = 钩子的错：忽略后照常执行，主循环无感。"""
        def boom(name, raw, ctx):
            raise RuntimeError("钩子炸了")
        self.agent.pre_tool_hooks.append(boom)
        r = self.run_tool("read_file", {"path": "a.txt"})
        self.assertTrue(r["ok"])
        self.assertIn("hello", r["result"])  # read_file 输出带行号栏

    # ---- post 钩子：改写 ------------------------------------------------

    def test_post_hook_rewrites_result_chained(self):
        """post 链式折叠：第一个改写的结果成为第二个的输入；None = 保持原样。

        改写 JSON 信封必须保持其合法（前端/trace/recent_reads 都按 JSON 解析）——
        正确姿势是解析-修改-重新序列化，测试按这个姿势写。"""
        def audit1(name, args, result):
            envelope = json.loads(result)
            envelope["result"] = str(envelope.get("result") or "") + "\n【审计①】"
            return json.dumps(envelope, ensure_ascii=False)
        self.agent.post_tool_hooks.append(audit1)
        self.agent.post_tool_hooks.append(
            lambda name, args, result: result if "审计①" in result else None)
        r = self.run_tool("read_file", {"path": "a.txt"})
        self.assertTrue(r["ok"])
        self.assertTrue(r["result"].endswith("【审计①】"))  # ② 看到 ① 的改写后放行

    def test_post_hook_exception_and_none_keep_result(self):
        """抛异常与返回 None 的钩子都不改结果。"""
        self.agent.post_tool_hooks.append(lambda *a: 1 / 0)
        self.agent.post_tool_hooks.append(lambda *a: None)
        r = self.run_tool("read_file", {"path": "a.txt"})
        self.assertTrue(r["ok"])
        self.assertIn("hello", r["result"])

    def test_post_hooks_skipped_for_parse_failure(self):
        """解析失败/否决的调用没有"真实产物"：post 不该拿到伪结果。"""
        seen = []
        self.agent.post_tool_hooks.append(
            lambda name, args, result: seen.append((name, args)) or None)
        bad = json.loads(self.agent._run_tool(
            {"function": {"name": "read_file", "arguments": "{bad json"}}))
        self.assertFalse(bad["ok"])
        self.assertEqual(seen, [])  # 解析失败：post 未运行

    # ---- 内置 recent_reads 钩子（原 _run_tool 内联逻辑的迁移）----------

    def test_builtin_recent_read_records_success_only(self):
        r_ok = self.run_tool("read_file", {"path": "a.txt"})
        self.assertTrue(r_ok["ok"])
        self.assertEqual([p for p, _ in self.agent.recent_reads], ["a.txt"])
        self.run_tool("read_file", {"path": "missing.txt"})  # 失败不入列
        self.assertEqual(len(self.agent.recent_reads), 1)

    def test_builtin_recent_read_trims_to_capacity(self):
        for i in range(RECENT_READS_KEEP + 3):  # 超容量 3 条：最旧的被裁掉
            p = self.ws / f"f{i}.txt"
            p.write_text(str(i), encoding="utf-8")
            self.run_tool("read_file", {"path": f"f{i}.txt"})
        paths = [p for p, _ in self.agent.recent_reads]
        self.assertEqual(len(paths), RECENT_READS_KEEP)
        self.assertNotIn("f0.txt", paths)
        self.assertEqual(paths[-1], f"f{RECENT_READS_KEEP + 2}.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
