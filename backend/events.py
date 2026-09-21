"""
SSE 常驻事件流的核心逻辑（纯逻辑，无 HTTP 依赖，单测见 test_sse.py）
=====================================================================

「每轮一个流」改为「命令接口 + 常驻事件流」后，回合过程事件（delta /
tool_call / done / …）不再借 POST 的响应流带回，而是全部进【每会话一条】
的事件总线（SessionEvents），由 GET /api/sessions/<sid>/events 接出的常驻
SSE 连接消费。断线重连的一切正确性都落在本模块的三件事上：seq 分配、
环形缓冲、补发计划（replay_plan）。模块不依赖 http.server，test_sse.py
直接对其单测。

三类角色，哪些进缓冲、哪些不进（混入会造成重复补发/挤掉真实事件）：
  1. 业务事件（answer_delta / tool_call / done / turn_start / turn_end …）：
     只能经 publish() 进——它统一分配 seq、写入环形缓冲、分发在线订阅者。
     这是唯一写入路径，任何旁路直拼 SSE 帧都会破坏"重连不丢不重"；
  2. 心跳注释行（": ping"，每 15 秒）：仅由 HTTP 层写在网络连接上，防代理/
     隧道（Cloudflare 等）掐掉空闲连接。它不分配 seq、不进缓冲、不进订阅
     队列——它对任何重连者都没有回放价值，进缓冲只会白占一个格子；
  3. 连接态事件（resync / caught_up）：只在某条连接的补发阶段有意义，对其
     他连接是噪音，同样不进缓冲，由 HTTP 层直写（且不写 id: 行，避免污染
     浏览器自动维护的 Last-Event-ID 游标）。

seq 与 mid 是两套身份，绝不混用：seq 是【事件】在会话内单调递增的流水号
（SSE 的 id: 行；浏览器 EventSource 断线重连自动携带 Last-Event-ID），mid
是【消息】的稳定身份（存储层用）。seq 持久化（sessions.last_seq）的目的
只有一个：重启后新事件不与旧 seq 撞号——重启后内存缓冲必然为空、落后于
计数器的客户端一律走 resync 全量刷新，因此持久化只需保证"不回退"。
"""

import json
import logging
import threading
from collections import deque
from queue import Queue

log = logging.getLogger("events")

# 环形缓冲容量：按"一次长回合的事件量"取量级。一轮几十个 delta + 若干工具
# 事件通常远小于它；超长回合（上千 delta）挤掉回合开头时，replay_plan 退
# 化为"尽力补尾巴"而不是 resync（见其 docstring 的推导）。
REPLAY_BUFFER_SIZE = 500

# SSE 心跳注释行。SSE 规范里冒号开头是注释，客户端解析层直接忽略，但字节
# 走到了网络上，代理的空闲计时器会被重置——这就是它防掐连接的全部原理。
SSE_HEARTBEAT = b": ping\n\n"


def sse_frame(seq: int | None, event: dict) -> bytes:
    """把一条事件编成 SSE 帧。业务事件带 seq → 写 id: 行（浏览器记为
    Last-Event-ID，自动重连时带回）；连接态事件（resync/caught_up）传
    seq=None → 不写 id，不污染客户端的续传游标。"""
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append("data: " + json.dumps(event, ensure_ascii=False))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class SessionEvents:
    """一个会话的事件总线：seq 计数器 + 环形缓冲 + 订阅者分发。

    线程模型：回合 worker 是唯一发布者（会话锁串行），每条 events 连接是
    一个订阅者（每标签页一个）。内部一把锁只保护计数器/缓冲/订阅者名单
    三个字段的读写瞬间，队列分发与 persist 落库都在锁外——Queue 自身线程
    安全，而锁内做 DB I/O 会把发布者（生成回路的必经点）卡在订阅者操作上。
    """

    def __init__(self, last_seq: int = 0, buffer_size: int = REPLAY_BUFFER_SIZE,
                 persist=None):
        self._seq = int(last_seq)          # 从持久化值起步（重启不归零）
        self._buffer: deque = deque(maxlen=buffer_size)  # (seq, event) 环形缓冲
        self._subs: list[Queue] = []       # 在线订阅者，每条 events 连接一个
        self._lock = threading.Lock()
        self._persist = persist            # fn(seq)：最新 seq 落库，可缺省
        # 回合边界状态（由 turn_start/turn_end 事件顺带维护）：
        # replay_plan 靠它们把"正在进行的回合"从起点整段补发。
        self.running = False
        self._round_seq: int | None = None  # 本回合 turn_start 的 seq

    # ---------- 读取 ----------

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    def buffered(self) -> list[tuple[int, dict]]:
        """缓冲快照（测试与补发计划用），按 seq 升序。"""
        with self._lock:
            return list(self._buffer)

    # ---------- 发布（唯一写入路径） ----------

    def publish(self, event: dict) -> int:
        """发布一条业务事件：分配 seq → 入环形缓冲 → 分发订阅者 → 落库。

        分发先于落库：订阅者看到事件的时刻不依赖 DB；落库失败只打日志不
        阻断（重启兜底本来就是 resync，回退几个 seq 号无碍正确性——参见
        模块 docstring 对"只需不撞号"的论证）。返回分配到的 seq。
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            item = (seq, event)
            self._buffer.append(item)
            # 回合边界顺带记账：补发计划要识别"正在进行的回合"（refresh
            # 接上正在输出的回合、断线从回合中段续传，都靠这两个字段）
            etype = event.get("type")
            if etype == "turn_start":
                self.running = True
                self._round_seq = seq
            elif etype == "turn_end":
                self.running = False
            subs = list(self._subs)
        for q in subs:
            q.put(item)
        if self._persist is not None:
            try:
                self._persist(seq)
            except Exception as e:  # 落库失败不影响事件流
                log.warning("last_seq 落库失败（seq=%s）：%s", seq, e)
        return seq

    # ---------- 订阅 ----------

    def subscribe(self) -> Queue:
        """注册一个订阅者（一条 events 连接），返回它的专属队列。"""
        q: Queue = Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass  # 已移除（连接收尾与 close 并发时的正常情况）

    def close(self) -> None:
        """会话被删除时收摊：向所有在线连接塞 None 哨兵，events 处理循环
        收到后结束连接——否则它们会一直挂在心跳上占着线程。"""
        with self._lock:
            subs, self._subs = self._subs, []
            self.running = False
        for q in subs:
            q.put(None)

    # ---------- 断线补发计划 ----------

    def replay_plan(self, position: int | None) -> tuple[str, list[tuple[int, dict]]]:
        """计算补发计划。position = 客户端已看到的最大 seq（None = 全新
        观看者，什么都没看过，历史由分页接口负责）。返回 (模式, 事件列表)：

          ("live", [])    无缺口，直接续实时流
          ("replay", xs)  先补发 xs（seq 升序）再续实时流
          ("resync", [])  缺口补不齐：HTTP 层向客户端发 resync 事件（带
                          current_seq），客户端全量刷新后以它为锚重连

        规则按客户端状态推导（前两类是"断线重连"，后两类是"刷新/新观看"）：
        1. position > current_seq：只可能出现在服务重启后（seq 从持久化值
           恢复，可能落后于客户端见过的值）→ resync；
        2. 缓冲覆盖不了 position（最老 seq > position+1）：中间的事件已被
           挤掉，补不齐 → resync。特例：缓冲为空（刚重启）时 position 恰好
           等于 current_seq 则什么都没漏，续流即可；
        3. 回合进行中且 turn_start 仍在缓冲：从 min(position+1, turn_start)
           补起。position 落在回合中间（刷新页面接上正在输出的回合）时连
           回合开头一起补——客户端有 seq 闸门，已应用过的部分会跳过，多发
           无害；少发才致命（缺 turn_start 就画不出用户气泡和过程时间线）；
        4. position 为 None（全新观看者）：只补正在进行的回合（若有）——
           turn_start 之前的都是"库里的过去"，分页接口负责。
        回合进行中但 turn_start 已被挤出缓冲（超长回答 > 缓冲容量）：从
        max(position+1, 最老seq) 补"尽力而为的尾巴"，绝不 resync——resync
        后客户端重连时 turn_start 依然不在缓冲，只会无限 resync 循环。丢
        回合开头的展示（done 事件仍带完整回答，最终会补齐）比死循环轻。
        """
        with self._lock:
            cur = self._seq
            oldest = self._buffer[0][0] if self._buffer else None
            round_seq = self._round_seq if self.running else None

            if position is not None and (position > cur or position < 0):
                return "resync", []

            if position is None:
                if round_seq is None or oldest is None:
                    return "live", []  # 无进行中回合/空缓冲：历史靠分页接口
                if oldest <= round_seq:
                    return "replay", [it for it in self._buffer if it[0] >= round_seq]
                return "replay", list(self._buffer)  # 开头被挤出：尽力补尾巴

            if not self._buffer:
                # 缓冲为空（典型：服务刚重启）。停在最新位置 = 什么都没漏
                return ("live", []) if position == cur else ("resync", [])

            start = position + 1
            if round_seq is not None:
                start = (min(start, round_seq) if oldest <= round_seq
                         else max(start, oldest))
            if start < oldest:
                return "resync", []  # position 与缓冲之间有被挤掉的缺口
            items = [it for it in self._buffer if it[0] >= start]
            return ("replay", items) if items else ("live", [])
