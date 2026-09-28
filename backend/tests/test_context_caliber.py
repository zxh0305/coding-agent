"""上下文徽章口径的持久化兜底测试（cd backend && python3 -m unittest tests.test_context_caliber -v）

真实事故：手动停止一个还没收到任何 usage 的回合（秒停），落库的 assistant
消息 usage 全 0；之后服务重启，内存 _ctx 为空，/api/context 回查
db.latest_context_tokens——旧实现取"ord 最大消息的 prompt_tokens"，0 IS NOT NULL
成立，徽章显示 0/100万（上下文里明明还有几万 token 的历史）。

两层修复的回归面：
1. agent._tail_answer / _tail_stopped 把 metrics.context_tokens（最后一次
   请求的真实 prompt = 模型当前上下文大小）写进 _stats，随 stats_json 落库
   ——此前这个值只活在内存 _ctx 里，重启即失；
2. db.latest_context_tokens 优先读 stats_json.context_tokens，跳过 0/None
   （0 是"还没收到用量"不是"上下文为空"）；老消息没有该字段时降级用非零
   累计 prompt_tokens（历史被重复计数的旧口径，聊胜于无）；压缩边界之前
   的一律不可信（代表压缩前的上下文，比现在大）。
"""

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import db
from agent import Agent


class ContextCaliberTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctx_caliber_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        db.renumbered_sessions.clear()
        self.addCleanup(setattr, db, "DB_PATH", self._orig)
        self.addCleanup(db.renumbered_sessions.clear)

    def _persist(self, msgs):
        saved = {}
        db.save_messages("s1", msgs, saved)
        return saved

    def _stopped_round(self, usage, context_tokens):
        """走真实 _tail_stopped 路径构造一条"手动停止"的 assistant 消息。"""
        agent = Agent(llm=None, verbose=False, workspace=str(self.tmp / "ws"))
        agent.history = [{"role": "user", "content": "问"}]
        metrics = {"start": time.time(), "cache_hit_rate": None,
                   "context_tokens": context_tokens, "context": None}
        list(agent._tail_stopped(None, usage, metrics))  # 生成器：驱动到 done 收尾
        return agent.history

    def test_stopped_empty_round_does_not_shadow_context(self):
        """回归主体：秒停空回合（usage 全 0、metrics 无 context）落库后，
        兜底查询仍返回此前真实轮的 context_tokens，而不是 0。"""
        # 上一轮：真实跑过若干请求，最后一次 prompt=15000（累计 44000）
        real = {"role": "assistant", "content": "回答",
                "_stats": {"usage": {"prompt_tokens": 44000, "completion_tokens": 8000,
                                     "total_tokens": 52000},
                           "context_tokens": 15000}}
        self._persist([{"role": "user", "content": "问"}, real])
        # 秒停轮：一个 usage 都没收到
        self._persist(self._stopped_round(
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            context_tokens=None))

        self.assertEqual(db.latest_context_tokens("s1"), 15000)

    def test_stopped_round_persists_its_own_context(self):
        """停止前已收到过 usage：context_tokens 随 _stats 落库，重启后可用。"""
        self._persist(self._stopped_round(
            {"prompt_tokens": 3000, "completion_tokens": 200, "total_tokens": 3200},
            context_tokens=3000))
        self.assertEqual(db.latest_context_tokens("s1"), 3000)

    def test_legacy_rows_fall_back_to_nonzero_accumulated(self):
        """老消息（无 context_tokens 字段）：降级用非零累计 prompt_tokens。"""
        legacy = {"role": "assistant", "content": "老回答",
                  "_stats": {"usage": {"prompt_tokens": 12345, "completion_tokens": 100,
                                       "total_tokens": 12445}}}
        self._persist([{"role": "user", "content": "问"}, legacy])
        self.assertEqual(db.latest_context_tokens("s1"), 12345)

    def test_zero_accumulated_not_mistaken_for_context(self):
        """只有 usage 全 0 的消息（且无 context_tokens）：返回 None 而非 0。"""
        self._persist(self._stopped_round(
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            context_tokens=None))
        self.assertIsNone(db.latest_context_tokens("s1"))

    def test_rows_before_compact_boundary_ignored(self):
        """压缩边界之前的值不可信（代表压缩前的上下文）：边界后没有可信值返回 None。"""
        real = {"role": "assistant", "content": "回答",
                "_stats": {"usage": {"prompt_tokens": 44000, "completion_tokens": 8000,
                                     "total_tokens": 52000},
                           "context_tokens": 15000}}
        boundary = {"role": "compact", "content": "摘要", "is_compact_boundary": True,
                    "_stats": {"compacted": True}}
        self._persist([{"role": "user", "content": "问"}, real, boundary,
                       {"role": "assistant", "content": "新回答",
                        "_stats": {"usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                             "total_tokens": 0}}}])
        self.assertIsNone(db.latest_context_tokens("s1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
