"""
停止路由修复与回退编辑的单元测试（cd backend && python3 -m unittest tests.test_edit_resend -v）
==============================================================================================

两件事，都来自真实踩坑：

1. 停止路由（app._stop_target）：生成中切模型会重建 _agents[sid]，旧回合
   仍持旧实例在跑——停止请求若只查 _agents，开关置到没人听的新实例上，
   旧请求直到 60s 超时才收场（实测 37 秒"停止中…"）。修复 = 回合开始时
   在 _running_agents 登记真正在跑的实例，停止请求优先查它。
2. 回退编辑（db.truncate_from，ZCode editUserQuery 的 rewind 语义，V1 不带
   文件回卷）：删除某条用户消息及其后的全部消息——消息行、token 用量行、
   执行过程轨迹、外置归档文件一并清理；已被压缩进摘要的旧消息拒绝回退
   （摘要引用会悬空；ZCode 对此走 fork，V1 直接拒绝）。
"""

import json
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


def tool_call_asst(call_id):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": "run_bash", "arguments": "{}"}}]}


def tool_result(text, call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def boundary(summary):
    return {"role": "compact", "content": summary, "is_compact_boundary": True,
            "_stats": {"compacted": True}}


class TruncateTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="edit_resend_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        db.renumbered_sessions.clear()
        self.addCleanup(setattr, db, "DB_PATH", self._orig)
        self.addCleanup(db.renumbered_sessions.clear)

    def row(self, sql, *params):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()


class TestTruncateFrom(TruncateTestBase):

    def _build_history(self):
        """6 条历史：u1 a1 u2 a2（带外置归档的消息） u3 a3。"""
        big = {"role": "assistant", "content": "巨" * (db.MAX_INLINE_BYTES + 1)}
        hist = [user("问题1"), asst("回答1"),
                user("问题2"), tool_call_asst("c1"), tool_result("结果1", "c1"),
                big, user("问题3"), asst("回答3")]
        saved = {}
        db.save_messages("s1", hist, saved)
        return hist, saved, big

    def test_truncates_from_user_message_inclusive(self):
        """从"问题2"回退：问题2 及其后（含 tool 配对、归档消息、问题3/回答3）
        全部删除；问题1/回答1 原样保留；返回删除行数与切点 ord。"""
        hist, saved, big = self._build_history()
        target_mid = hist[2]["_mid"]
        out = db.truncate_from("s1", target_mid)
        self.assertEqual(out["removed"], 6)  # 问题2、tool_call、tool_result、big、问题3、回答3
        msgs = db.get_messages("s1")
        self.assertEqual([m["content"] for m in msgs if m["role"] in ("user", "assistant")
                          and not m.get("_artifact")], ["问题1", "回答1"])
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])

    def test_usage_traces_and_artifact_file_cleaned(self):
        """被删消息的 token 用量行、执行轨迹、外置归档文件一并清理。"""
        hist, saved, big = self._build_history()
        hist[3]["_stats"] = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        db.save_messages("s1", hist, saved)
        target_mid = hist[2]["_mid"]
        artifact_path = db.DB_PATH.parent / "artifacts" / big["path"]
        self.assertTrue(artifact_path.exists())
        from db import set_trace
        set_trace("s1", hist[2]["_mid"], json.dumps([{"type": "round"}]))

        db.truncate_from("s1", target_mid)
        self.assertFalse(artifact_path.exists(), "归档文件随消息清理")
        self.assertEqual(self.row("SELECT COUNT(*) c FROM message_usage WHERE session_id='s1'")[0]["c"], 0)
        self.assertEqual(self.row("SELECT COUNT(*) c FROM session_traces WHERE session_id='s1'")[0]["c"], 0)

    def test_rejects_non_user_and_missing_mid(self):
        """只能回退到自己发出的消息；不存在的 mid 拒绝。"""
        hist, saved, _ = self._build_history()
        with self.assertRaises(ValueError, msg="assistant 消息不是合法回退点"):
            db.truncate_from("s1", hist[1]["_mid"])
        with self.assertRaises(ValueError):
            db.truncate_from("s1", "no-such-mid")

    def test_rejects_target_before_compact_boundary(self):
        """已被压缩进摘要的旧消息拒绝回退：删掉原文，摘要引用就悬空了。"""
        hist, saved, _ = self._build_history()
        hist.insert(2, boundary("摘要"))
        db.save_messages("s1", hist, saved)
        with self.assertRaises(ValueError, msg="边界之前的消息不许回退"):
            db.truncate_from("s1", hist[0]["_mid"])
        # 边界之后的消息照常可回退
        out = db.truncate_from("s1", hist[3]["_mid"])
        self.assertGreater(out["removed"], 0)

    def test_ord_gap_not_renumbered(self):
        """回退不动保留消息的 ord（增量落盘的账本不被打乱）：保留段 ord 原值。"""
        hist, saved, _ = self._build_history()
        ords_before = {m["_mid"]: m["_ord"] for m in hist if m.get("_mid")}
        db.truncate_from("s1", hist[6]["_mid"])
        for m in db.get_messages("s1"):
            self.assertEqual(m["_ord"], ords_before[m["_mid"]])


class TestStopTarget(TruncateTestBase):
    """停止路由：优先命中正在跑回合的实例，而非当前缓存的实例。"""

    def test_running_agent_wins_over_rebuilt_cache(self):
        import app
        stale = Agent(llm=None, verbose=False, workspace=self.tmp / "ws1")
        fresh = Agent(llm=None, verbose=False, workspace=self.tmp / "ws2")
        old = app._agents.get("sX")
        old_running = dict(app._running_agents)
        try:
            app._agents["sX"] = stale          # 切模型后缓存里是新实例
            app._running_agents["sX"] = fresh  # 但真正在跑的是旧实例
            self.assertIs(app._stop_target("sX"), fresh,
                          "停止必须打到正在跑回合的实例")
            app._running_agents.pop("sX")      # 回合结束登记摘除后
            self.assertIs(app._stop_target("sX"), stale, "回落到缓存实例")
        finally:
            app._agents["sX"] = old
            app._running_agents.clear()
            app._running_agents.update(old_running)

    def test_round_registers_and_unregisters(self):
        """_run_round 开始时登记、收尾后摘除（finally 路径，错误回合也是）。"""
        import threading
        import app

        class StuckLLM:
            def chat_stream(self, messages, tools=None, cancel=None):
                # 等待停止信号：保证 _run_round 跑到中途时登记一定已发生
                cancel.wait(5)
                yield "message", {"role": "assistant", "content": "停了"}

        agent = Agent(llm=StuckLLM(), verbose=False, workspace=str(self.tmp / "ws3"))
        db.create_session("sX", 1)
        seen = {}

        def runner():
            app._run_round("sX", agent, "问", {"role": "user", "content": "问"}, "n1", [])
            seen["running_after"] = app._running_agents.get("sX")

        old_agents = dict(app._agents)
        old_running = dict(app._running_agents)
        try:
            app._agents["sX"] = agent
            t = threading.Thread(target=runner)
            t.start()
            t.join(10)
            self.assertIsNone(seen.get("running_after"), "收尾后登记必须摘除")
            self.assertIsNone(app._running_agents.get("sX"))
        finally:
            app._agents.clear()
            app._agents.update(old_agents)
            app._running_agents.clear()
            app._running_agents.update(old_running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
