"""派生物标注（_derived）的单测：清理旧工具结果时留下可追溯的改写记录。

为什么值得测：之前清理工具结果只留一个布尔标记，排查「模型为什么说看不到
命令输出」时无法回答"何时清的、原来多长"，只能去怀疑外置归档或前端渲染。
_derived 就是给这类问题留的账；同时它有两条硬约束不能被破坏：

  1. 绝不能发给模型（带下划线的内部字段一律剥离，部分服务商会 400）；
  2. 外置归档（大消息换成摘要行）时不能丢——否则大结果一归档就查不出原因。
"""

import unittest

import db


class DerivedMarkTest(unittest.TestCase):
    def test_storable_body_keeps_derived(self):
        """_derived 必须进落库内容，否则重启后追溯记录全丢。"""
        m = {"role": "tool", "tool_call_id": "c1", "content": "占位符",
             "_derived": {"tool_result_cleared": {"at": 1, "original_chars": 999}}}
        body = db._storable_body(m)
        self.assertIn("_derived", body)
        self.assertEqual(body["_derived"]["tool_result_cleared"]["original_chars"], 999)

    def test_storable_body_still_strips_identity(self):
        """_mid/_ord/_stats 仍按原规则剥离（身份在列里，统计在另一张表）。"""
        m = {"role": "assistant", "content": "x", "_mid": "m1", "_ord": 3,
             "_stats": {"in": 1}, "_derived": {"a": 1}}
        body = db._storable_body(m)
        for key in ("_mid", "_ord", "_stats"):
            self.assertNotIn(key, body)
        self.assertIn("_derived", body)

    def test_clean_outgoing_strips_derived(self):
        """发给模型的视图里不能出现内部字段（否则部分服务商直接 400）。"""
        m = {"role": "tool", "tool_call_id": "c1", "content": "占位符",
             "_derived": {"tool_result_cleared": {"at": 1, "original_chars": 999}},
             "_tool_result_cleared": True}
        clean = self._clean(m)
        self.assertNotIn("_derived", clean)
        self.assertNotIn("_tool_result_cleared", clean)
        self.assertEqual(clean["content"], "占位符")

    @staticmethod
    def _clean(m):
        # 直接复用 Agent 的实现，保证测的是线上那份逻辑而不是复刻品
        from agent import Agent
        return Agent._clean_outgoing(m)

    def test_original_is_never_persisted(self):
        """_derived.original 只是进程内的退路，绝不能落库——否则清理白做，
        几万字符的原文又被存了一份。"""
        m = {"role": "tool", "tool_call_id": "c1", "content": "占位符",
             "_derived": {"tool_result_cleared": {"at": 1, "original_chars": 5000},
                          "original": "很长的原文" * 1000}}
        body = db._storable_body(m)
        self.assertNotIn("original", body["_derived"])
        self.assertIn("tool_result_cleared", body["_derived"])   # 账还要留

    def test_derived_dropped_entirely_when_only_original(self):
        m = {"role": "tool", "content": "x", "_derived": {"original": "abc"}}
        self.assertNotIn("_derived", db._storable_body(m))

    def test_restore_cleared_recovers_original(self):
        """清理后能无损还原：这是"清理不可逆"的退路。"""
        import shutil
        import tempfile
        from agent import Agent

        ws = tempfile.mkdtemp(prefix="restore_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=None, verbose=False, workspace=ws)
        agent.history = [{"role": "user", "content": "开始"}]
        original = "命令输出" * 2000
        for i in range(6):
            agent.history.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            })
            agent.history.append({"role": "tool", "tool_call_id": f"c{i}",
                                  "content": original})
        cleared = agent._clear_old_tool_results(keep_recent=1)
        self.assertGreater(cleared, 0)
        restored = agent.restore_cleared()
        self.assertEqual(restored, cleared)
        # 被清的那几条必须逐字节回到原样
        restored_contents = [m["content"] for m in agent.history
                             if m.get("role") == "tool"]
        self.assertEqual(sum(1 for c in restored_contents if c == original), 6)
        self.assertFalse(any(m.get("_tool_result_cleared") for m in agent.history))

    def test_restore_is_noop_without_originals(self):
        import shutil
        import tempfile
        from agent import Agent

        ws = tempfile.mkdtemp(prefix="restore_ws2_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=None, verbose=False, workspace=ws)
        agent.history = [{"role": "tool", "content": "原样"}]
        self.assertEqual(agent.restore_cleared(), 0)
        self.assertEqual(agent.history[0]["content"], "原样")

    def test_restore_respects_size_filter(self):
        import shutil
        import tempfile
        from agent import Agent

        ws = tempfile.mkdtemp(prefix="restore_ws3_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=None, verbose=False, workspace=ws)
        agent.history = [
            {"role": "tool", "content": "占位A",
             "_derived": {"tool_result_cleared": {"at": 1}, "original": "短"},
             "_tool_result_cleared": True},
            {"role": "tool", "content": "占位B",
             "_derived": {"tool_result_cleared": {"at": 1}, "original": "长" * 500},
             "_tool_result_cleared": True},
        ]
        self.assertEqual(agent.restore_cleared(only_chars_above=100), 1)
        self.assertEqual(agent.history[0]["content"], "占位A")     # 短的没动
        self.assertEqual(agent.history[1]["content"], "长" * 500)

    def test_clear_old_tool_results_records_derivation(self):
        """真实的清理路径必须留下 _derived 账：清了几条、原来多长。"""
        import shutil
        import tempfile
        from agent import Agent

        ws = tempfile.mkdtemp(prefix="derived_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        agent = Agent(llm=None, verbose=False, workspace=ws)
        # 造 6 条足够大的工具结果（超过最小节省阈值）
        agent.history = [{"role": "user", "content": "开始"}]
        for i in range(6):
            agent.history.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            })
            agent.history.append({"role": "tool", "tool_call_id": f"c{i}",
                                  "content": "输出内容" * 2000})
        cleared_n = agent._clear_old_tool_results(keep_recent=1)
        self.assertGreater(cleared_n, 0, "样本足够大时必须真的清掉一些")
        marked = [m for m in agent.history if m.get("_derived", {}).get("tool_result_cleared")]
        self.assertEqual(len(marked), cleared_n)
        rec = marked[0]["_derived"]["tool_result_cleared"]
        self.assertGreater(rec["original_chars"], 0)
        self.assertGreater(rec["at"], 0)


if __name__ == "__main__":
    unittest.main()
