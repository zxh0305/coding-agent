"""
常驻事件流（SSE 断线重连）纯逻辑的单元测试
==========================================

cd backend && python3 -m unittest test_sse -v

覆盖 events.SessionEvents 与 seq 持久化的核心不变式（跑在临时库里，不碰
项目根的 agent_data.db）：
1. seq 单调递增、每条落库；模拟重启（内存丢弃、仅剩 sessions.last_seq）后
   从持久化值继续，绝不归零；
2. since 命中缓冲 → 尾部补发序列正确（seq 升序、内容一致）；
3. since 过旧（缓冲挤掉缺口）/ 超前于计数器（重启回退）→ resync；
4. 心跳不进缓冲（不占 seq、不占格子），业务事件全部进缓冲；
5. 缓冲满后挤掉最老事件（环形 deque 语义）；
6. 进行中回合的补发扩展：从 turn_start 整段补（刷新场景）；
   turn_start 已被挤出时退化为尽力补尾巴（绝不 resync 死循环）；
7. 订阅分发：多订阅者都收到、退订后不再收、close 哨兵唤醒；
8. SSE 帧格式：业务事件带 id: 行，连接态事件与心跳不带。
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from queue import Queue

import db
from events import SSE_HEARTBEAT, SessionEvents, sse_frame


class SSETestBase(unittest.TestCase):
    """每个用例独占一个临时库（重定向 db.DB_PATH，模式同 test_storage）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sse_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp) / "test.db"
        db.init_db()
        db.create_session("s1", 1)
        db.renumbered_sessions.clear()

    def tearDown(self):
        db.DB_PATH = self._orig_db_path

    def wired_bus(self, sid: str = "s1", **kw) -> SessionEvents:
        """接上真实持久化的总线：persist 每条事件写 sessions.last_seq。"""
        return SessionEvents(last_seq=db.get_last_seq(sid),
                             persist=lambda seq: db.set_last_seq(sid, seq), **kw)


# ---------------------------------------------------------------------------
# 一、seq：单调递增 + 持久化 + 重启续接
# ---------------------------------------------------------------------------

class TestSeqPersistence(SSETestBase):

    def test_seq_monotonic_and_persisted_every_event(self):
        """每发布一条 seq +1 且立刻落库——WAL 下一行 UPDATE 是微秒级，
        换"客户端见过的 seq 重启后绝不回退"的强保证。"""
        bus = self.wired_bus()
        seqs = [bus.publish({"type": "answer_delta", "delta": "字"}) for _ in range(5)]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        self.assertEqual(db.get_last_seq("s1"), 5)

    def test_restart_continues_from_persisted_value(self):
        """模拟重启：进程内的计数器与缓冲全部丢失，仅剩库里的 last_seq。
        新总线必须从持久化值继续（下一条 seq=8），重启后绝不归零——否则
        新事件与客户端见过的旧事件撞号，前端 seq 闸门会把新事件误丢。"""
        bus = self.wired_bus()
        for _ in range(7):
            bus.publish({"type": "x"})
        bus2 = self.wired_bus()  # "重启"：从库恢复
        self.assertEqual(bus2.current_seq, 7)
        self.assertEqual(bus2.publish({"type": "x"}), 8)

    def test_last_seq_column_added_by_migration(self):
        """迁移 8 幂等加列：全新库 init_db 后 sessions 带 last_seq（老库
        由 PRAGMA user_version + 探针走同一条 ALTER）。"""
        with sqlite3.connect(db.DB_PATH) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        self.assertIn("last_seq", cols)
        db.init_db()  # 重跑幂等：不报错、不重复加列
        with sqlite3.connect(db.DB_PATH) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        self.assertEqual(sum(c == "last_seq" for c in cols), 1)

    def test_set_last_seq_never_goes_backwards(self):
        """标量 MAX：乱序到达的旧值不能把计数器拉回去。"""
        db.set_last_seq("s1", 10)
        db.set_last_seq("s1", 3)  # 迟到的旧值
        self.assertEqual(db.get_last_seq("s1"), 10)


# ---------------------------------------------------------------------------
# 二、三、补发计划：命中缓冲 / 过旧 resync / 回合扩展 / 尾巴兜底
# ---------------------------------------------------------------------------

class TestReplayPlan(SSETestBase):

    def test_since_in_buffer_replays_tail_in_order(self):
        """since 命中缓冲：补发所有 seq > since 的事件，顺序与内容一致。"""
        bus = SessionEvents(last_seq=0)
        for i in range(10):
            bus.publish({"type": "delta", "i": i})
        mode, items = bus.replay_plan(4)
        self.assertEqual(mode, "replay")
        self.assertEqual([s for s, _ in items], [5, 6, 7, 8, 9, 10])
        self.assertEqual([e["i"] for _, e in items], [4, 5, 6, 7, 8, 9])

    def test_since_at_counter_is_pure_live(self):
        """since == 计数器：什么都没漏，直接续流（空补发）。"""
        bus = SessionEvents()
        for _ in range(3):
            bus.publish({"type": "x"})
        self.assertEqual(bus.replay_plan(3), ("live", []))

    def test_since_too_old_resync_when_not_running(self):
        """since 早于缓冲最老 seq（中间有被挤掉的事件）：补不齐 → resync，
        绝不静默跳号。"""
        bus = SessionEvents(buffer_size=5)
        for _ in range(8):
            bus.publish({"type": "x"})          # 缓冲只剩 4..8
        self.assertEqual(bus.replay_plan(2)[0], "resync")   # 缺 seq3
        self.assertEqual(bus.replay_plan(3)[0], "replay")   # oldest-1：恰好接上
        mode, items = bus.replay_plan(3)
        self.assertEqual([s for s, _ in items], [4, 5, 6, 7, 8])

    def test_position_ahead_of_counter_resync_after_restart(self):
        """重启回退场景：客户端见过的 seq 超过恢复后的计数器 → resync
        （新事件不能与旧 seq 撞号后还装作续上了）。"""
        bus = SessionEvents(last_seq=10)  # 重启后从持久化值恢复，缓冲为空
        self.assertEqual(bus.replay_plan(11)[0], "resync")

    def test_empty_buffer_position_equals_counter_is_live(self):
        """重启后缓冲为空但客户端恰好停在最新位置：什么都没漏，续流即可
        （其余情况一律 resync，见下一例）。"""
        bus = SessionEvents(last_seq=10)
        self.assertEqual(bus.replay_plan(10), ("live", []))
        self.assertEqual(bus.replay_plan(9)[0], "resync")

    def test_running_round_replays_from_turn_start(self):
        """刷新页面接上正在输出的回合：position 落在回合中段，补发从
        turn_start 整段开始（客户端按自己的 seq 闸门跳过已应用部分）。"""
        bus = SessionEvents()
        bus.publish({"type": "turn_start", "input": "问"})   # seq 1
        for _ in range(4):
            bus.publish({"type": "answer_delta"})            # 2..5
        mode, items = bus.replay_plan(3)
        self.assertEqual(mode, "replay")
        self.assertEqual([s for s, _ in items], [1, 2, 3, 4, 5])
        self.assertEqual(bus.running, True)

    def test_running_round_position_before_round_replays_from_position(self):
        """断线发生在回合开始之前：min(position+1, turn_start) 取 position+1
        ——回合之前的老尾巴照常补（客户端可能正等着上一回合的 done 收尾）。"""
        bus = SessionEvents()
        bus.publish({"type": "turn_start"})   # 1
        bus.publish({"type": "done"})         # 2
        bus.publish({"type": "turn_end"})     # 3（上一回合结束）
        bus.publish({"type": "turn_start"})   # 4（新回合开始，进行中）
        bus.publish({"type": "answer_delta"}) # 5
        mode, items = bus.replay_plan(2)      # 客户端看到 2（done 之后掉线）
        self.assertEqual([s for s, _ in items], [3, 4, 5])   # turn_end 必须补到手

    def test_turn_start_evicted_replays_best_effort_tail_no_loop(self):
        """超长回合挤掉 turn_start：退化为补尾巴而不是 resync——resync 后
        客户端重连时 turn_start 依然不在缓冲，只会无限 resync 循环。"""
        bus = SessionEvents(buffer_size=5)
        bus.publish({"type": "turn_start"})                   # seq 1，将被挤出
        for _ in range(7):
            bus.publish({"type": "answer_delta"})             # 2..8 → 缓冲 4..8
        self.assertEqual(bus.running, True)
        mode, items = bus.replay_plan(6)
        self.assertEqual(mode, "replay")
        self.assertEqual([s for s, _ in items], [7, 8])
        # 落后很多也一样：从最老缓冲事件起尽力补，不 resync
        mode, items = bus.replay_plan(2)
        self.assertEqual(mode, "replay")
        self.assertEqual([s for s, _ in items], [4, 5, 6, 7, 8])

    def test_fresh_viewer_only_gets_running_round(self):
        """全新观看者（since 缺省）：只补正在进行的回合，turn_start 之前的
        "过去"由分页接口负责，不补发。"""
        bus = SessionEvents()
        bus.publish({"type": "turn_start"})   # 1
        bus.publish({"type": "done"})         # 2
        bus.publish({"type": "turn_end"})     # 3
        self.assertEqual(bus.replay_plan(None), ("live", []))  # 无进行中回合
        bus.publish({"type": "turn_start"})   # 4
        bus.publish({"type": "answer_delta"}) # 5
        mode, items = bus.replay_plan(None)
        self.assertEqual(mode, "replay")
        self.assertEqual([s for s, _ in items], [4, 5])


# ---------------------------------------------------------------------------
# 四、五、缓冲内容：业务进 / 心跳不进 / 满了挤最老
# ---------------------------------------------------------------------------

class TestBuffer(SSETestBase):

    def test_business_events_in_buffer_heartbeat_not(self):
        """业务事件全部进缓冲并占 seq；心跳（SSE_HEARTBEAT）只是网络层
        注释行，不经过总线——不占 seq、不占缓冲格子，否则会顶掉真实事件
        且补发时被重放。"""
        bus = SessionEvents()
        bus.subscribe()
        types = ["turn_start", "answer_delta", "reasoning_delta",
                 "tool_call", "tool_result", "usage", "done", "turn_end"]
        for t in types:
            bus.publish({"type": t})
        # 心跳在此期间持续写在网络连接上（字节串拼接模拟），对总线零影响
        wire = SSE_HEARTBEAT * 20
        self.assertEqual(len(wire), 160)  # ": ping\n\n" 8 字节 × 20，全部只走了网络
        buffered = bus.buffered()
        self.assertEqual([e["type"] for _, e in buffered], types)
        self.assertEqual([s for s, _ in buffered], list(range(1, 9)))

    def test_buffer_evicts_oldest_when_full(self):
        """环形 deque(maxlen)：满了挤掉最老事件，不静默跳号——被挤掉的
        事件由 replay_plan 的 resync 兜底。"""
        bus = SessionEvents(buffer_size=3)
        for i in range(5):
            bus.publish({"type": "x", "i": i})
        buffered = bus.buffered()
        self.assertEqual([s for s, _ in buffered], [3, 4, 5])   # 最老的两个被挤掉
        self.assertEqual([e["i"] for _, e in buffered], [2, 3, 4])
        self.assertEqual(bus.current_seq, 5)  # 计数器不随挤压回退


# ---------------------------------------------------------------------------
# 六、订阅分发
# ---------------------------------------------------------------------------

class TestSubscribers(SSETestBase):

    def test_all_subscribers_receive_and_unsubscribe_stops(self):
        """多标签页 = 多订阅者：人人收到同一事件；退订后不再收。"""
        bus = SessionEvents()
        q1, q2 = bus.subscribe(), bus.subscribe()
        bus.publish({"type": "answer_delta", "delta": "a"})
        self.assertEqual(q1.get(timeout=1), (1, {"type": "answer_delta", "delta": "a"}))
        self.assertEqual(q2.get(timeout=1), (1, {"type": "answer_delta", "delta": "a"}))
        bus.unsubscribe(q1)
        bus.publish({"type": "done"})
        self.assertEqual(q2.get(timeout=1)[0], 2)
        self.assertTrue(q1.empty())

    def test_close_wakes_all_subscribers_with_sentinel(self):
        """会话删除：close 向所有在线连接塞 None 哨兵——events 处理循环
        靠它退出，否则连接线程会一直挂在心跳上。"""
        bus = SessionEvents()
        q = bus.subscribe()
        bus.close()
        self.assertIsNone(q.get(timeout=1))
        self.assertEqual(bus.running, False)

    def test_publish_after_close_is_harmless(self):
        """删除瞬间的在途回合还会 publish：不炸、事件只是无人消费
        （缓冲照进，随总线一起被丢弃）。"""
        bus = SessionEvents()
        bus.close()
        self.assertEqual(bus.publish({"type": "turn_end"}), 1)


# ---------------------------------------------------------------------------
# 七、SSE 帧格式
# ---------------------------------------------------------------------------

class TestSSEFrame(unittest.TestCase):

    def test_business_event_has_id_line(self):
        """业务事件：id: <seq> + data: <json>——浏览器据 id 维护 Last-Event-ID。"""
        frame = sse_frame(42, {"type": "answer_delta", "delta": "字"})
        self.assertEqual(frame, b"id: 42\ndata: "
                                b'{"type": "answer_delta", "delta": "\xe5\xad\x97"}\n\n')

    def test_connection_scoped_event_has_no_id(self):
        """连接态事件（resync/caught_up）：不带 id——写了会污染客户端的
        Last-Event-ID 续传游标。"""
        frame = sse_frame(None, {"type": "resync", "seq": 7})
        self.assertFalse(frame.startswith(b"id:"))
        self.assertTrue(frame.startswith(b"data: "))

    def test_heartbeat_is_comment_line(self):
        """心跳是 SSE 注释行（冒号开头）：客户端解析层忽略，代理会重置
        空闲计时器——防掐连接的全部原理。"""
        self.assertEqual(SSE_HEARTBEAT, b": ping\n\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
