"""
会话可重建不变量（cd backend && python3 -m unittest tests.test_reconstruction -v）
==================================================================================

借鉴 DeepSeek Harness 的核心设计断言：「发给模型的请求必须能从落库日志重建」。
本项目的数据模型与此同构——DB 是唯一真相，内存历史/模型视图都是它的投影——
但这条不变式此前只是注释里的纪律，这里把它变成可执行的断言：

  1. 视图重建：save_messages 落盘 → restore_window 恢复 → 换一个 Agent 实例
     构建 _messages_for_model()，与落盘前原实例的模型视图【逐字节】一致；
     覆盖：assistant.tool_calls、tool 结果、多部分 user（视觉输入）、_stats、
     超大消息外置（_artifact 摘要行 + 归档还原）、压缩边界（锚点 + 摘要 + 活区）。
  2. 账本重建：恢复的消息经 db.fingerprints 重算出的指纹账本，与落盘时的
     账本逐条一致——重启后第一轮 save_messages 只写新增（不误判"全变了"
     重写全表），从恢复点继续对话写入了恰好"新增条数"行。
  3. 合成消息边界：_synthetic（收尾指令/回合提醒）只活在内存、不落库——
     DB 仍是时间线唯一真相。

全部跑在临时库里（重定向 db.DB_PATH），不发网络请求。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import db
from agent import Agent


class _NullLLM:
    """Agent 构造冒烟用：重建路径只动本地消息与本地归档，不发起任何请求。"""


def user(text):
    return {"role": "user", "content": text}


def calls_msg(call_id, name, args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def tool_result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def boundary(summary):
    """压缩边界标记（agent.py _maybe_compact 落进 history 的形状）。"""
    return {"role": "compact", "content": summary, "is_compact_boundary": True,
            "_stats": {"compacted": True}}


def rich_history(with_boundary: bool):
    """覆盖全部存储形态的一段历史。超大 tool 结果 > MAX_INLINE_BYTES(65536)。"""
    msgs = [
        user("修复 demo.py 的 bug 并验证"),
        calls_msg("call_1", "read_file", {"path": "demo.py"}),
        tool_result("call_1", "def main():\n    return 2 + 2\n"),
        {**calls_msg("call_2", "run_bash", {"command": "python3 demo.py"}),
         "_stats": {"usage": {"prompt_tokens": 1200, "completion_tokens": 40,
                              "total_tokens": 1240},
                    "provider_id": "prov", "model": "test-model"}},
        tool_result("call_2", "PAD" * 40000),  # 80000 字节正文 → 触发外置归档
        {"role": "assistant", "content": "已修复并运行验证通过。"},
        user([{"type": "text", "text": "再看这张截图"},
              {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]),
    ]
    if with_boundary:
        msgs.append(boundary("【早期对话已压缩】用户要求修复 demo.py；已修复并验证。"))
        msgs.append(user("继续，把函数名改清晰"))
        msgs.append(calls_msg("call_3", "apply_patch", {"search": "main", "replace": "run"}))
        msgs.append(tool_result("call_3", '{"ok": true}'))
        msgs.append({"role": "assistant", "content": "改名完成。"})
    return msgs


class ReconstructionTestBase(unittest.TestCase):
    """每个用例独占一个临时库：重定向 db.DB_PATH 后 init_db（同 test_storage）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recon_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        db.renumbered_sessions.clear()

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    def make_agent(self):
        agent = Agent(llm=_NullLLM(), verbose=False, workspace=str(self.tmp),
                      artifact_reader=db.read_artifact)
        return agent


# ---------------------------------------------------------------------------
# 一、视图重建：落盘 → 恢复 → 模型视图逐字节一致
# ---------------------------------------------------------------------------

class TestViewModelReconstruction(ReconstructionTestBase):

    def _roundtrip_view(self, msgs):
        """跑完整链路，返回 (落盘前视图, 恢复后视图, 落盘行数)。"""
        agent_a = self.make_agent()
        agent_a.history = [dict(m) for m in msgs]  # 拷贝一层，保护用例间的原始构造
        view_a = agent_a._messages_for_model()
        written = db.save_messages("s1", agent_a.history, agent_a.saved)
        restored = db.restore_window("s1")
        agent_b = self.make_agent()
        agent_b.history = restored
        view_b = agent_b._messages_for_model()
        return view_a, view_b, written

    def test_plain_history_reconstructs_byte_for_byte(self):
        """未压缩会话：恢复后的模型视图与落盘前逐条一致（含 tool 配对与视觉输入）。"""
        msgs = rich_history(with_boundary=False)
        view_a, view_b, written = self._roundtrip_view(msgs)
        self.assertEqual(written, len(msgs))
        self.assertEqual(view_b, view_a)

    def test_compacted_history_reconstructs_byte_for_byte(self):
        """压缩过会话：锚点 + 边界 + 活区恢复出的视图与全量内存视图一致——
        restore_window 的"锚点必须捎上"与 _visible_history 的视图规则在此闭环。"""
        msgs = rich_history(with_boundary=True)
        view_a, view_b, _ = self._roundtrip_view(msgs)
        self.assertEqual(view_b, view_a)
        # 视图确实走了压缩投影：首条原始需求逐字在座，中段被摘要替代
        self.assertIn("修复 demo.py", view_b[0]["content"])
        self.assertTrue(any("早期对话已压缩" in str(m.get("content")) for m in view_b))

    def test_externalized_message_content_survives_roundtrip(self):
        """超大消息外置后，恢复视图里的正文与原文完全一致（不是 head/tail 拼接降级）。"""
        msgs = rich_history(with_boundary=False)
        view_a, view_b, _ = self._roundtrip_view(msgs)
        original = next(m for m in view_a if m.get("role") == "tool"
                        and str(m.get("content", "")).startswith("PAD"))
        rebuilt = next(m for m in view_b if m.get("role") == "tool"
                       and str(m.get("content", "")).startswith("PAD"))
        self.assertEqual(rebuilt["content"], original["content"])
        self.assertEqual(len(rebuilt["content"]), 40000 * 3)

    def test_stats_and_internal_fields_never_reach_the_model(self):
        """恢复消息带回的 _mid/_ord/_stats 必须在模型视图外被剥净（服务商拒收未知字段）。"""
        msgs = rich_history(with_boundary=False)
        _, view_b, _ = self._roundtrip_view(msgs)
        for m in view_b:
            self.assertFalse([k for k in m if k.startswith("_")], m)
            self.assertNotIn("_stats", m)


# ---------------------------------------------------------------------------
# 二、账本重建：指纹一致 → 重启后续轮只写增量
# ---------------------------------------------------------------------------

class TestLedgerReconstruction(ReconstructionTestBase):

    def test_fingerprints_rebuild_matches_saved_ledger(self):
        """恢复消息重算的指纹与落盘账本逐条一致（指纹=落库字节，同用 _storable_body）。"""
        agent_a = self.make_agent()
        agent_a.history = rich_history(with_boundary=True)
        db.save_messages("s1", agent_a.history, agent_a.saved)
        rebuilt = db.fingerprints(db.restore_window("s1"))
        self.assertTrue(rebuilt)
        for mid, fp in rebuilt.items():
            self.assertEqual(agent_a.saved.get(mid), fp, mid)
        # 窗口恢复只覆盖锚点+边界后的消息：边界之前的 mid 不要求在账本里，
        # 但恢复窗口内的每一条都必须能对上（save 只会查询历史里出现过的 mid）。

    def test_continuation_after_restart_writes_only_new_rows(self):
        """重启模拟：恢复历史 + 指纹账本重建后，续写一轮只落新增的 2 行。"""
        agent_a = self.make_agent()
        agent_a.history = rich_history(with_boundary=True)
        db.save_messages("s1", agent_a.history, agent_a.saved)

        agent_b = self.make_agent()
        agent_b.history = db.restore_window("s1")
        agent_b.saved = dict(db.fingerprints(agent_b.history))  # app.py 会话恢复的做法
        agent_b.history.append(user("再跑一遍测试"))
        agent_b.history.append({"role": "assistant", "content": "通过。"})
        written = db.save_messages("s1", agent_b.history, agent_b.saved)
        self.assertEqual(written, 2)
        # 再存一遍完全不动：增量落盘对已落库内容幂等
        self.assertEqual(db.save_messages("s1", agent_b.history, agent_b.saved), 0)


# ---------------------------------------------------------------------------
# 三、合成消息边界：_synthetic 只活在内存
# ---------------------------------------------------------------------------

class TestSyntheticNeverPersists(ReconstructionTestBase):

    def test_synthetic_message_is_not_stored_and_memory_untouched(self):
        """收尾指令/回合提醒（_synthetic）不落库；save 也不从内存历史里摘除它——
        它是回合进行时的请求成分，DB 只存真实时间线。"""
        agent = self.make_agent()
        agent.history = [user("问题"),
                         {"role": "user", "content": "【回合提醒】请尽快收尾", "_synthetic": True}]
        written = db.save_messages("s1", agent.history, agent.saved)
        self.assertEqual(written, 1)
        self.assertEqual(db.count_messages("s1"), 1)
        self.assertEqual(len(agent.history), 2)  # 内存原样
        restored = db.restore_window("s1")
        self.assertEqual([m["role"] for m in restored], ["user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
