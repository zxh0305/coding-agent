"""回退编辑（rewind）回归测试（cd backend && python3 -m unittest tests.test_rewind_regression -v）

三个回归面。db 层与停止路由的基础语义已由 tests/test_edit_resend.py 盖住
（截断含 tool 配对/用量/轨迹/归档清理、压缩边界拒绝、保留段 ord 不动、
停止命中运行中实例），这里补它没盖住的三处：

1. 目标消息自己是外置用户消息的清理。现有用例外置的都是 assistant 巨文本
   回答，回退目标从未外置过——而带大附件/超长正文的用户消息（外置后时间线
   走 artifact 分支，即 c38414f 修的"缺导航锚"消息形态）是附件重度用户的
   常态。回退删除它时归档文件必须随行清理；回退后重发同样内容要拿到全新的
   mid 与归档路径，旧归档不复活。
2. 截断后的增量账本延续。_assign_ords 的尾部追加从"最后一个已知 ord"起按
   ORD_GAP 递增（db.py），截断恰好改变这个基准：删光全部后要能从干净状态
   重新落账；截中段后追加的新消息 ord 必须全部大于保留段最大值，且与保留段
   无并列（ord 无唯一约束，并列会让分页游标漏条目）。
3. /api/sessions/<sid>/truncate 端点（app.py _handle_truncate）。HTTP 层的
   守卫与内存同步无用例：缺 mid 400；db 层 ValueError 转 400；有回合在跑
   409 且不动库不发事件；成功路径内存 agent.history/指纹账本与库同口径
   裁剪，history_truncated 事件发布并持久化 seq（断线重连补发的账本不倒退）。
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
from agent import Agent


def user(text):
    return {"role": "user", "content": text}


def asst(text):
    return {"role": "assistant", "content": text}


class RewindTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rewind_reg_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        # s1 给 db 层用例，s2 给端点用例（_event_bus 按 sid 缓存，错开避免
        # 跨类共享事件流状态）
        db.create_session("s1", 1)
        db.create_session("s2", 1)
        db.renumbered_sessions.clear()
        self.addCleanup(setattr, db, "DB_PATH", self._orig)
        self.addCleanup(db.renumbered_sessions.clear)

    def rows(self, sql, *params):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()


class TestExternalizedUserMessageCleanup(RewindTestBase):
    """回归面 1：回退目标是外置归档的用户消息。"""

    def _build(self):
        big = user("巨" * (db.MAX_INLINE_BYTES + 1))
        hist = [user("问题1"), asst("回答1"), big, user("问题3"), asst("回答3")]
        db.save_messages("s1", hist, {})
        return hist

    def test_target_artifact_removed_with_message(self):
        """回退点是外置用户消息：artifacts 归档文件随消息删除，保留段原样。"""
        hist = self._build()
        self.assertTrue(hist[2].get("_artifact"), "长正文用户消息应被外置")
        artifact = db.DB_PATH.parent / "artifacts" / hist[2]["path"]
        self.assertTrue(artifact.exists())

        out = db.truncate_from("s1", hist[2]["_mid"])

        self.assertEqual(out["removed"], 3)
        self.assertFalse(artifact.exists(), "外置归档必须随消息清理")
        kept = db.get_messages("s1")
        self.assertEqual([m["content"] for m in kept if not m.get("_artifact")],
                         ["问题1", "回答1"])

    def test_resend_after_rewind_writes_fresh_artifact(self):
        """回退后重发同样长的问题：新 mid、新归档路径，旧归档不复活。"""
        hist = self._build()
        old_path = hist[2]["path"]
        old_artifact = db.DB_PATH.parent / "artifacts" / old_path
        db.truncate_from("s1", hist[2]["_mid"])

        again = user("巨" * (db.MAX_INLINE_BYTES + 1))
        db.save_messages("s1", [again, asst("回答")], {})

        self.assertTrue(again.get("_artifact"), "重发的长正文同样外置")
        self.assertNotEqual(again["path"], old_path, "归档路径不能撞旧文件")
        self.assertFalse(old_artifact.exists(), "旧归档已随回退清理，不得复活")
        self.assertEqual([m["content"] for m in db.get_messages("s1")
                          if not m.get("_artifact")], ["问题1", "回答1", "回答"])


class TestOrdLedgerAfterTruncate(RewindTestBase):
    """回归面 2：截断后 ord 增量账本的延续。"""

    def test_truncate_all_then_new_round(self):
        """从第一条消息回退删光全部：新一轮照常落盘，ord 单调、mid 唯一。"""
        hist = [user("问题1"), asst("回答1")]
        db.save_messages("s1", hist, {})
        out = db.truncate_from("s1", hist[0]["_mid"])
        self.assertEqual(out["removed"], 2)
        self.assertEqual(db.get_messages("s1"), [])

        db.save_messages("s1", [user("新问题"), asst("新回答")], {})
        rows = db.get_messages("s1")
        self.assertEqual([m["content"] for m in rows], ["新问题", "新回答"])
        ords = [m["_ord"] for m in rows]
        self.assertEqual(ords, sorted(ords), "ord 单调")
        self.assertEqual(len({m["_mid"] for m in rows}), 2, "mid 唯一")

    def test_append_after_partial_truncate(self):
        """截中段后追加新一轮：保留段 ord 不动，新消息 ord 接在其后、无并列。"""
        hist = [user("问题1"), asst("回答1"), user("问题2"), asst("回答2"),
                user("问题3"), asst("回答3")]
        db.save_messages("s1", hist, {})
        kept_ords = [m["_ord"] for m in hist[:2]]

        out = db.truncate_from("s1", hist[2]["_mid"])
        self.assertEqual(out["removed"], 4)

        db.save_messages("s1", [user("新问题"), asst("新回答")], {})
        rows = db.get_messages("s1")
        self.assertEqual([m["_ord"] for m in rows[:2]], kept_ords,
                         "保留段 ord 不动")
        new_ords = [m["_ord"] for m in rows[2:]]
        self.assertEqual(new_ords, sorted(new_ords))
        self.assertTrue(all(o > kept_ords[-1] for o in new_ords),
                        "新 ord 必须接在保留段之后，不得与保留段并列")


class TestTruncateEndpoint(RewindTestBase):
    """回归面 3：/truncate 端点的守卫、内存同步与事件发布。"""

    SID = "s2"

    def _round(self):
        hist = [user("问题1"), asst("回答1"), user("问题2"), asst("回答2")]
        db.save_messages(self.SID, hist, {})
        return hist

    def _call(self, body):
        """直调 Handler._handle_truncate：handler 只用 self._body/_json，
        假身足够，不必起 HTTP 服务。"""
        sent = []

        class Req:
            def _body(self):
                return body

            def _json(self, obj, status=200):
                sent.append((status, obj))
                return obj

        import app
        app.Handler._handle_truncate(Req(), self.SID)
        return sent

    def test_missing_mid_rejected(self):
        sent = self._call({})
        self.assertEqual(sent, [(400, {"error": "缺少 mid"})])
        self.assertEqual(db.get_messages(self.SID), [])

    def test_non_user_mid_rejected(self):
        """db 层 ValueError（只能回退到自己发出的消息）转 400，库不动。"""
        hist = self._round()
        sent = self._call({"mid": hist[1]["_mid"]})
        self.assertEqual(len(sent), 1)
        status, payload = sent[0]
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertEqual(len(db.get_messages(self.SID)), 4)

    def test_rejects_while_round_running(self):
        """有回合在跑：409 拦截，库不动、事件不发（防御前端 streaming 判断失灵）。"""
        import app
        hist = self._round()
        before_seq = db.get_last_seq(self.SID)
        old_running = dict(app._running_agents)
        try:
            app._running_agents[self.SID] = object()  # 占位：表示有回合在跑
            sent = self._call({"mid": hist[0]["_mid"]})
        finally:
            app._running_agents.clear()
            app._running_agents.update(old_running)
        self.assertEqual(sent, [(409, {"error": "任务正在运行，请先停止再回退"})])
        self.assertEqual(len(db.get_messages(self.SID)), 4, "运行中拒绝不动库")
        self.assertEqual(db.get_last_seq(self.SID), before_seq, "拒绝不发布事件")

    def test_success_syncs_agent_memory_and_publishes(self):
        """成功路径：内存 agent.history/指纹账本与库同口径裁剪，事件发布且
        seq 持久化（其他标签页/断线重连靠它补发 history_truncated）。"""
        import app
        hist = self._round()
        agent = Agent(llm=None, verbose=False, workspace=str(self.tmp / "ws"))
        agent.history = [dict(m) for m in hist]
        agent.saved = {m["_mid"]: "fp" for m in hist if m.get("_mid")}
        old = app._agents.get(self.SID)
        app._agents[self.SID] = agent
        try:
            before_seq = db.get_last_seq(self.SID)
            sent = self._call({"mid": hist[2]["_mid"]})
        finally:
            if old is None:
                app._agents.pop(self.SID, None)
            else:
                app._agents[self.SID] = old

        self.assertEqual(sent, [(200, {"ok": True, "removed": 2})])
        self.assertEqual([m["content"] for m in agent.history],
                         ["问题1", "回答1"], "内存历史与库同口径裁剪")
        self.assertEqual(set(agent.saved),
                         {hist[0]["_mid"], hist[1]["_mid"]}, "指纹账本随内存裁剪")
        self.assertEqual(len(db.get_messages(self.SID)), 2)
        self.assertGreater(db.get_last_seq(self.SID), before_seq,
                           "history_truncated 已发布且 seq 持久化")


if __name__ == "__main__":
    unittest.main(verbosity=2)
