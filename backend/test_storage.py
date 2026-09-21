"""
会话存储改造的单元测试（cd backend && python3 -m unittest test_storage -v）
================================================================================

覆盖四块改造的核心不变式（全部跑在临时库里，不碰项目根的 agent_data.db）：
1. 稳定身份 + 增量落盘：连续 20 轮保存，第二轮起写入行数 = 新增条数；
   重启模拟（指纹账本由恢复的历史重建）后重存 0 写入；
2. 压缩边界插入：ord 取中点、单调无错位、无重复 mid，get_messages 顺序正确；
3. 大内容外置：超阈值消息行 < 65536 字节，归档文件与原文逐字节一致，
   read_artifact 拒绝一切越界路径（../、绝对路径、非 .json、嵌套 ../）；
4. schema 迁移：老表（无 mid 列）重建后数据可读、条数不变、顺序保持；
   迁移跑两遍结果一致（幂等）；
另测：_stats 拆分到 message_usage 并在读取时回填；窗口恢复（锚点 + 边界后）；
模型视图对窗口内 _artifact 消息的还原（窗口外不还原）。
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


def asst(text, stats=None):
    m = {"role": "assistant", "content": text}
    if stats is not None:
        m["_stats"] = stats
    return m


def boundary(summary):
    """压缩边界标记（agent.py _maybe_compact 落进 history 的形状）。"""
    return {"role": "compact", "content": summary, "is_compact_boundary": True,
            "_stats": {"compacted": True}}


class StorageTestBase(unittest.TestCase):
    """每个用例独占一个临时库：重定向 db.DB_PATH 后 init_db。

    db 的所有函数都在调用时读模块级 DB_PATH，重定向即隔离；artifacts
    目录跟 DB_PATH.parent 走，同样落在临时目录里。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="storage_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    def row(self, sql, *params):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute(sql, params).fetchone()


# ---------------------------------------------------------------------------
# 一、稳定身份 + 增量落盘
# ---------------------------------------------------------------------------

class TestIncrementalSave(StorageTestBase):

    def test_twenty_rounds_only_write_new_rows(self):
        """连续 20 轮：每轮新增 2 条，写入行数必须恒等于 2（旧方案的
        replace_messages 每轮重写全表，第 r 轮要写 2r 行——这正是要根治的写放大）。"""
        saved, history = {}, []
        for r in range(1, 21):
            history.append(user(f"问题{r}"))
            history.append(asst(f"回答{r}"))
            written = db.save_messages("s1", history, saved)
            self.assertEqual(written, 2, f"第 {r} 轮应只写 2 行，实际 {written}")
        self.assertEqual(db.count_messages("s1"), 40)
        self.assertEqual(len(saved), 40)  # 指纹账本与库内行数一致

    def test_resave_same_history_writes_zero(self):
        """同一份历史原样再存：0 写入、无重复行（幂等，主键保证）。"""
        history = [user("问"), asst("答")]
        saved = {}
        db.save_messages("s1", history, saved)
        self.assertEqual(db.save_messages("s1", history, saved), 0)
        self.assertEqual(db.count_messages("s1"), 2)

    def test_fingerprint_rebuilt_after_restart_writes_zero(self):
        """重启模拟：进程内的 saved 丢失，由恢复的历史重建（db.fingerprints）
        后重存必须 0 写入——指纹算法与落库序列化不一致时这里会全表重写。"""
        history = [user("问1"), asst("答1"), user("问2"), asst("答2")]
        saved = {}
        db.save_messages("s1", history, saved)
        restored = db.get_messages("s1")
        self.assertEqual([m["content"] for m in restored],
                         ["问1", "答1", "问2", "答2"])
        saved2 = db.fingerprints(restored)
        self.assertEqual(db.save_messages("s1", restored, saved2), 0)

    def test_no_orphan_rows_after_session_deleted(self):
        """会话被删后落盘直接放弃：不留孤儿消息行（保留旧 replace_messages 的防线）。"""
        history = [user("问")]
        saved = {}
        db.save_messages("s1", history, saved)
        db.delete_session("s1")
        history.append(asst("答"))
        self.assertEqual(db.save_messages("s1", history, saved), 0)
        self.assertEqual(db.count_messages("s1"), 0)

    def test_changed_content_rewrites_same_mid(self):
        """同 mid 内容变了（指纹不同）：按 INSERT OR REPLACE 原位重写，不产生重复行。"""
        history = [user("旧内容")]
        saved = {}
        db.save_messages("s1", history, saved)
        mid = history[0]["_mid"]
        history[0]["content"] = "新内容"
        self.assertEqual(db.save_messages("s1", history, saved), 1)
        self.assertEqual(db.count_messages("s1"), 1)  # 没有第二条
        self.assertEqual(db.get_messages("s1")[0]["_mid"], mid)  # 身份不变


# ---------------------------------------------------------------------------
# 二、压缩边界插入：ord 无错位
# ---------------------------------------------------------------------------

class TestCompactBoundary(StorageTestBase):

    def test_boundary_insert_keeps_ord_monotonic_and_unique(self):
        """插入 role=compact 边界后继续追加：ord 单调递增、无重复 mid、
        get_messages 顺序与预期时间线一致（旧方案里位置即身份，插入即错位）。"""
        history = [user(f"消息{i}") for i in range(1, 11)]  # 先落 10 条
        saved = {}
        db.save_messages("s1", history, saved)
        history.insert(4, boundary("前 4 条的摘要"))  # 模拟 _maybe_compact 的中段插入
        history.append(user("边界之后的新问题"))       # 压缩后继续对话
        written = db.save_messages("s1", history, saved)
        self.assertEqual(written, 2)  # 只写边界 + 新问题，被后移的旧消息一行不重写

        msgs = db.get_messages("s1")
        self.assertEqual([m["content"] for m in msgs],
                         [f"消息{i}" for i in range(1, 5)] + ["前 4 条的摘要"]
                         + [f"消息{i}" for i in range(5, 11)] + ["边界之后的新问题"])
        ords = [m["_ord"] for m in msgs]
        self.assertEqual(ords, sorted(ords))  # 单调不减
        self.assertEqual(len(ords), len(set(ords)))  # 无重复
        mids = [m["_mid"] for m in msgs]
        self.assertEqual(len(mids), len(set(mids)))  # 无重复 mid

    def test_restore_window_matches_view_of_full_history(self):
        """窗口恢复（锚点 + 最后一条边界及其之后）构造的模型视图，与全量恢复
        逐字节一致——这是"内存省了，模型看到的没变"的直接验证。"""
        history = [user("原始需求：修 demo.py")]
        history += [user(f"问题{i}") for i in range(1, 6)]
        history += [asst(f"回答{i}") for i in range(1, 6)]
        saved = {}
        db.save_messages("s1", history, saved)
        history.insert(6, boundary("早期摘要"))
        history += [user("边界后问题"), asst("边界后回答")]
        db.save_messages("s1", history, saved)

        window = db.restore_window("s1")
        self.assertLess(len(window), db.count_messages("s1"))  # 确实没全量进内存
        # 视图一致性：窗口历史与全量历史各自喂给 Agent，发给模型的消息相同
        full_agent = Agent(llm=None, verbose=False, workspace=self.tmp)
        full_agent.history = history
        win_agent = Agent(llm=None, verbose=False, workspace=self.tmp)
        win_agent.history = window
        self.assertEqual(full_agent._messages_for_model(), win_agent._messages_for_model())


# ---------------------------------------------------------------------------
# 三、大内容外置
# ---------------------------------------------------------------------------

class TestArtifactExternalization(StorageTestBase):

    def test_1mb_content_externalized_and_byte_identical(self):
        """1MB 内容：行内只存摘要（< 65536），归档文件读回与原文逐字节一致；
        内存里的消息被就地替换成摘要行（不再扛着 1MB）。"""
        big = {"role": "assistant", "content": "大" * 1_048_576}
        history = [user("跑个会输出巨量日志的命令"), big]
        saved = {}
        db.save_messages("s1", history, saved)

        row_len, row_mid = self.row(
            "SELECT LENGTH(content), mid FROM messages WHERE role='assistant'")
        self.assertLess(row_len, 65536)  # 行不膨胀
        # 归档文件与"原文的规范序列化"逐字节一致
        expected = json.dumps({"role": "assistant", "content": "大" * 1_048_576},
                              ensure_ascii=False)
        artifact = (db.DB_PATH.parent / "artifacts" / "s1" / f"{row_mid}.json").read_text()
        self.assertEqual(artifact, expected)
        # 读回还原
        self.assertEqual(db.read_artifact(f"s1/{row_mid}.json"), json.loads(expected))
        # 内存历史已被替换成摘要行
        self.assertTrue(big.get("_artifact"))
        self.assertEqual(big["path"], f"s1/{row_mid}.json")
        # 摘要行重存（重启模拟）：指纹稳定，0 写入、归档文件不重写
        restored = db.get_messages("s1")
        self.assertTrue(restored[-1].get("_artifact"))
        self.assertEqual(db.save_messages("s1", restored, db.fingerprints(restored)), 0)

    def test_small_content_stays_inline(self):
        """小消息照旧内联进 content 列，不产生归档文件。"""
        history = [user("普通消息")]
        saved = {}
        db.save_messages("s1", history, saved)
        self.assertEqual(json.loads(self.row(
            "SELECT content FROM messages WHERE role='user'")[0])["content"], "普通消息")
        self.assertFalse((db.DB_PATH.parent / "artifacts" / "s1").exists())

    def test_read_artifact_rejects_escapes(self):
        """路径逃逸全拒绝：../、绝对路径、非 .json、嵌套 ../；合法相对路径放行。"""
        history = [user("x"), asst("y")]
        saved = {}
        db.save_messages("s1", history, saved)
        # 造一个合法归档当"参照物"
        big = {"role": "assistant", "content": "z" * (db.MAX_INLINE_BYTES + 1)}
        history.append(big)
        db.save_messages("s1", history, saved)
        self.assertEqual(db.read_artifact(big["path"])["content"], "z" * (db.MAX_INLINE_BYTES + 1))

        for evil in ("../agent_data.db", "../../.env", "/etc/passwd",
                     "s1/secret.txt", "a/../../b.json", "s1/../../s1.json", ""):
            with self.assertRaises(ValueError, msg=f"应拒绝 {evil!r}"):
                db.read_artifact(evil)

    def test_stats_split_into_usage_table(self):
        """_stats 不进 messages.content，落进 message_usage（token 列可查），
        get_messages 读取时回填——前端回放的耗时/token 统计不丢。"""
        stats = {"elapsed_s": 3.2, "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                                             "total_tokens": 140}, "cache_hit_rate": 55.5}
        history = [user("问"), asst("答", stats=stats)]
        saved = {}
        db.save_messages("s1", history, saved)
        content_json = self.row("SELECT content FROM messages WHERE role='assistant'")[0]
        self.assertNotIn("_stats", content_json)  # content 行不带统计
        pt, ct = self.row("SELECT prompt_tokens, completion_tokens FROM message_usage")
        self.assertEqual((pt, ct), (100, 40))
        self.assertEqual(db.get_messages("s1")[1]["_stats"], stats)  # 回填无损


# ---------------------------------------------------------------------------
# 四、schema 迁移
# ---------------------------------------------------------------------------

class TestMigrations(StorageTestBase):

    def _build_legacy_messages(self, n=6):
        """构造旧形态 messages 表（seq 自增主键 + idx 位置），塞 n 行样本。"""
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DROP TABLE messages")
            conn.execute(
                "CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, idx INTEGER NOT NULL, role TEXT NOT NULL, "
                "content TEXT NOT NULL)")
            conn.execute("PRAGMA user_version = 0")  # 旧库没有版本记账
            for i in range(n):
                conn.execute("INSERT INTO messages(session_id, idx, role, content) "
                             "VALUES('s1', ?, 'user', ?)", (i, json.dumps(
                                 {"role": "user", "content": f"旧消息{i}"}, ensure_ascii=False)))
            conn.commit()

    def test_legacy_table_migrated_readably(self):
        """无 mid 列的旧表：迁移后条数不变、内容可读、顺序保持、mid/ord 就位。"""
        self._build_legacy_messages(6)
        db.init_db()  # 触发迁移 6（建新表→复制→改名）
        self.assertEqual(db.count_messages("s1"), 6)
        msgs = db.get_messages("s1")
        self.assertEqual([m["content"] for m in msgs], [f"旧消息{i}" for i in range(6)])
        self.assertTrue(all(m["_mid"] and m["_ord"] == i * db.ORD_GAP for i, m in enumerate(msgs)))
        # 旧库数据迁完，继续增量追加照常工作
        saved = db.fingerprints(msgs)
        msgs.append(user("迁移后的新消息"))
        self.assertEqual(db.save_messages("s1", msgs, saved), 1)

    def test_migrations_idempotent(self):
        """迁移跑两遍结果一致：条数、mid 集合、user_version 都不变。"""
        self._build_legacy_messages(4)
        db.init_db()
        snap1 = (db.count_messages("s1"),
                 sorted(m["_mid"] for m in db.get_messages("s1")),
                 self.row("PRAGMA user_version")[0])
        db.init_db()  # 第二遍
        snap2 = (db.count_messages("s1"),
                 sorted(m["_mid"] for m in db.get_messages("s1")),
                 self.row("PRAGMA user_version")[0])
        self.assertEqual(snap1, snap2)

    def test_fresh_db_gets_final_schema_and_version(self):
        """全新库：直接建最终形态、版本号一步到位（无 legacy 表可迁）。"""
        self.assertEqual(self.row("PRAGMA user_version")[0], db.SCHEMA_VERSION)
        with sqlite3.connect(db.DB_PATH) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        self.assertTrue({"mid", "session_id", "ord", "role", "content"} <= cols)
        # (session_id, ord) 索引就位——分页与窗口加载的查询路径
        idx = self.row("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_ord'")
        self.assertIsNotNone(idx)


# ---------------------------------------------------------------------------
# 五、窗口加载与模型视图还原
# ---------------------------------------------------------------------------

class TestWindowedLoading(StorageTestBase):

    def _seed(self, n=10):
        history = [user(f"m{i}") for i in range(n)]
        saved = {}
        db.save_messages("s1", history, saved)
        return history

    def test_get_messages_full_and_windowed(self):
        """不传参 = 全量（兼容旧行为）；since_ord/before_ord/limit 各取所需，
        结果一律按 ord 升序。"""
        history = self._seed(10)
        ords = [m["_ord"] for m in db.get_messages("s1")]
        # 最近 3 条（升序返回）
        self.assertEqual([m["content"] for m in db.get_messages("s1", limit=3)],
                         ["m7", "m8", "m9"])
        # since_ord：该序及之后
        self.assertEqual([m["content"] for m in db.get_messages("s1", since_ord=ords[7])],
                         ["m7", "m8", "m9"])
        # before_ord + limit：向上翻页一页
        self.assertEqual([m["content"] for m in db.get_messages("s1", before_ord=ords[7], limit=3)],
                         ["m4", "m5", "m6"])
        # has_more 判断：最旧一条之前没有更早的；中间一条之前有
        self.assertFalse(db.has_messages_before("s1", ords[0]))
        self.assertTrue(db.has_messages_before("s1", ords[5]))

    def test_restore_window_loads_boundary_and_after_plus_anchor(self):
        """窗口恢复 = 锚点（首条 user）+ 最后一条边界及其之后；边界之前不进内存。"""
        history = [user("锚点")] + [user(f"m{i}") for i in range(8)]
        saved = {}
        db.save_messages("s1", history, saved)
        history.insert(5, boundary("第一段摘要"))
        db.save_messages("s1", history, saved)
        history.insert(8, boundary("第二段摘要（吸收第一段）"))
        history += [user("活区消息")]
        db.save_messages("s1", history, saved)

        window = db.restore_window("s1")
        total = db.count_messages("s1")
        # 最后一条边界之后 = [B2, m6, m7, 活区消息]，加锚点共 5 条；
        # 被它盖掉的 7 条（m0..m5、B1）不进内存
        self.assertEqual(len(window), 5)
        self.assertEqual(total, 12)
        self.assertEqual([m["content"] for m in window],
                         ["锚点", "第二段摘要（吸收第一段）", "m6", "m7", "活区消息"])
        self.assertNotIn("第一段摘要", [m.get("content") for m in window])  # 旧边界被吸收，不进窗口

    def test_restore_without_boundary_is_full(self):
        """从未压缩：窗口恢复退化为全量（兼容旧行为）。"""
        self._seed(5)
        self.assertEqual(len(db.restore_window("s1")), 5)


class TestModelViewExpansion(StorageTestBase):
    """模型视图不变式：外置只影响存储与前端，不改变模型看到的内容。"""

    def _agent(self, history, reader_calls=None):
        ws = tempfile.mkdtemp(prefix="mv_ws_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)

        def reader(path):
            if reader_calls is not None:
                reader_calls.append(path)
            return db.read_artifact(path)

        agent = Agent(llm=None, verbose=False, workspace=ws, artifact_reader=reader)
        agent.history = history
        return agent

    def _stubify(self, m):
        """把一条消息落盘成归档摘要行，返回摘要行（带 _mid/_ord）。"""
        saved = {}
        db.save_messages("s1", [m], saved)
        return m  # save_messages 已就地替换为摘要行

    def test_in_window_artifact_expanded_out_window_not(self):
        """窗口内的 _artifact 消息还原成完整内容；被压缩段的归档消息连恢复都
        不加载（在窗口之外），自然不触发读盘——还原只发生在可见窗口内。"""
        reader_calls = []
        big_a = {"role": "assistant", "content": "A" * (db.MAX_INLINE_BYTES + 1)}
        big_b = {"role": "assistant", "content": "B" * (db.MAX_INLINE_BYTES + 1)}
        saved = {}
        db.save_messages("s1", [user("锚点"), big_a], saved)
        db.save_messages("s1", [boundary("早期摘要"), big_b], saved)
        # 真实恢复路径：锚点 + 最后一条边界及其之后（big_a 属被压缩段，不进内存）
        window = db.restore_window("s1")
        self.assertEqual(len(window), 3)  # 锚点 + 边界 + big_b，共 4 条里的 3 条

        agent = self._agent(window, reader_calls)
        view = agent._messages_for_model()
        # big_b（窗口内）必须还原为完整内容
        self.assertTrue(any(m.get("content") == "B" * (db.MAX_INLINE_BYTES + 1) for m in view))
        # 只有窗口内的归档被读；big_a 的归档一次都不会碰
        self.assertEqual(reader_calls, [big_b["path"]])
        # 视图结构与 _visible_history 约定一致：锚点 + 摘要 + 活区
        self.assertEqual([m["role"] for m in view], ["user", "user", "assistant"])
        self.assertEqual(view[0]["content"], "锚点")
        self.assertIn("早期摘要", view[1]["content"])

    def test_expansion_failure_falls_back_to_preview(self):
        """归档文件缺失：退回 head/tail 文字并注明截断，绝不让读盘失败打断对话。"""
        m = self._stubify({"role": "assistant", "content": "全" * (db.MAX_INLINE_BYTES + 1)})
        (db.DB_PATH.parent / "artifacts" / "s1" / f"{m['_mid']}.json").unlink()
        agent = self._agent([user("问"), m])
        view = agent._messages_for_model()
        self.assertIn("读取失败", view[1]["content"])
        self.assertIn(m["head"][:50], view[1]["content"])  # head 预览还在


if __name__ == "__main__":
    unittest.main(verbosity=2)
