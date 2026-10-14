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
import time
from collections import deque
from queue import Empty, Full, Queue

log = logging.getLogger("events")

# 环形缓冲容量：按"一次长回合的事件量"取量级。一轮几十个 delta + 若干工具
# 事件通常远小于它；超长回合（上千 delta）挤掉回合开头时，replay_plan 退
# 化为"尽力补尾巴"而不是 resync（见其 docstring 的推导）。
REPLAY_BUFFER_SIZE = 500

# SSE 心跳注释行。SSE 规范里冒号开头是注释，客户端解析层直接忽略，但字节
# 走到了网络上，代理的空闲计时器会被重置——这就是它防掐连接的全部原理。
SSE_HEARTBEAT = b": ping\n\n"

# last_seq 落库的最小间隔（秒）。回合边界无条件落库，其余情况按此降频——
# 逐 token 落库是这个项目最大的一笔自找开销（见 publish 的论证）。
PERSIST_INTERVAL_S = 1.0

# 单个订阅者队列的容量上限。事件里 answer_delta 是逐 token 发布的，一个长回答
# 就是几千条——若客户端不读（笔记本休眠 / 网络挂起 / TCP 发送缓冲写满），
# 服务端的 SSE 线程会卡在 wfile.write 上不再取队列，无界队列就随发布者线性
# 增长（实测 20 万条 0.13s 进队，无任何背压），单个坏连接可把内存吃光甚至 OOM。
#
# 用法是"丢最旧的"而不是阻塞发布者：SSE 是实时流，客户端真正需要的永远是
# 最新状态；丢掉旧 delta 不会让会话错乱——前端本就按 mid 归并、且任何缺口都
# 走 resync 全量重连（见 sessions._handle_session_events 的缺口处理）。
# 取 5000：远超正常一回合的 delta 量（长回答约 2~5k），又远小于内存风险线。
SUBSCRIBER_QUEUE_MAX = 5000

# 丢事件警告的最小间隔（秒）。卡住的客户端会持续丢（逐 token 时每来一条丢一条），
# 逐条打会瞬间刷爆日志把信号埋掉——按时间降频，并附累计丢弃数。
DROP_WARN_INTERVAL_S = 5.0

# 同时打开的 SSE 连接上限。SSE 在 http.server 下是"一连接一线程"且**常驻**
# （直到客户端断开），实测 2600 条连接就逼近 macOS 默认 maxproc 2666——
# 打满后连【普通 API】也会被内核 RST，整个服务对所有用户不可用，而不是
# "只是 SSE 连不上"。因为占满线程的正是永不超时的 SSE 线程，它无法自愈。
#
# 取 256 的理由：远超单人自用工具的真实需求（几个标签页 + 手机端最多十几条），
# 又远低于任何平台的线程上限。超出上限的请求立刻收到 503 + 明确文案，客户端
# 按 SSE 自动重连退避后会成功，比"内核静默 RST"可控得多。
MAX_SSE_CONNECTIONS = 256

# 全局 SSE 连接计数信号量。放在这里（而非 app.py）是因为它保护的正是
# subscribe/unsubscribe 这对生命周期，两者挨着不易漂移。
SSE_SLOTS = threading.BoundedSemaphore(MAX_SSE_CONNECTIONS)


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
        # 上次落库的单调时刻（time.monotonic()）：last_seq 按 PERSIST_INTERVAL_S
        # 降频写，回合边界强制写一次。见 publish 的论证。
        self._persisted_at = time.monotonic()
        # 订阅者队列满而丢事件的累计数与上次告警时刻（告警按时间降频，
        # 见 publish 里的论证）：卡住的客户端会持续丢，逐条打会刷爆日志。
        self._dropped_total = 0
        self._drop_warned_at = 0.0
        # 回合边界状态（由 turn_start/turn_end 事件顺带维护）：
        # replay_plan 靠它们把"正在进行的回合"从起点整段补发。
        self.running = False
        self._round_seq: int | None = None  # 本回合 turn_start 的 seq
        # 本回合 turn_start 的墙钟时刻（time.time()）：补发/重连的客户端没有
        # 本地 qStart，只有它才能在摘要行继续显示「已工作 N 秒」而不是从 0 起。
        self._round_started: float | None = None

    # ---------- 读取 ----------

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def round_started_at(self) -> float | None:
        """进行中回合的起点墙钟时刻（无进行中回合则 None）。补发的客户端靠它
        续算「已工作 N 秒」——它没有本地的计时起点。"""
        with self._lock:
            return self._round_started if self.running else None

    def buffered(self) -> list[tuple[int, dict]]:
        """缓冲快照（测试与补发计划用），按 seq 升序。"""
        with self._lock:
            return list(self._buffer)

    # ---------- 发布（唯一写入路径） ----------

    def publish(self, event: dict) -> int:
        """发布一条业务事件：分配 seq → 入环形缓冲 → 分发订阅者 → 择机落库。

        分发先于落库：订阅者看到事件的时刻不依赖 DB；落库失败只打日志不
        阻断（重启兜底本来就是 resync，回退几个 seq 号无碍正确性——参见
        模块 docstring 对"只需不撞号"的论证）。返回分配到的 seq。

        【落库降频】last_seq 只在【回合边界】或距上次落库 PERSIST_INTERVAL_S
        以上才写。此前是每条事件都写一次，而 answer_delta 是逐 token 发布的
        ——一条 5000 token 的回答就是 5000 次 `sqlite3.connect` + 两条 PRAGMA
        + UPDATE + COMMIT，自用工具最大的一笔自找开销，并把 WAL 撑到数倍于
        库文件（现场：5MB 的库配 12MB 的 -wal）。降频不牺牲正确性：last_seq
        只用于"重启后 seq 不撞号"，回退几个号的最坏后果是补发时把已发过的
        帧再发一遍（前端按 mid 归并，幂等），而缓冲缺口本来就有 resync 兜底。
        进程被 kill 时最多丢最近 PERSIST_INTERVAL_S 内的事件计数——窗外无差。
        """
        should_persist = False   # 锁内赋真值；预置防锁内异常导致下方 NameError
        dropped = 0              # 本次发布因队列满丢弃的事件数（锁外统计）
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
                # 取事件自带时刻（缺省才用当下）：补发段里 turn_start 的发布
                # 时刻才是回合真正的起点，用「此刻」会把整段已过去的时间抹掉。
                ts = event.get("started_at")
                self._round_started = float(ts) if isinstance(ts, (int, float)) else time.time()
            elif etype == "turn_end":
                self.running = False
            subs = list(self._subs)
            # 是否落库（在锁内判定，避免多个发布者竞争同一刻度）
            boundary = etype in ("turn_start", "turn_end")
            now = time.monotonic()
            should_persist = (self._persist is not None
                              and (boundary or now - self._persisted_at >= PERSIST_INTERVAL_S))
            if should_persist:
                self._persisted_at = now
        for q in subs:
            # 【绝不阻塞发布者】SSE 是实时流，一个卡住的客户端不能拖住整个
            # 会话的事件分发（发布者就是正在跑回合的那个线程）。
            # 队列满 ⟹ 该客户端已经不读了（线程卡在 wfile.write），此时丢
            # 最旧的、留最新的：它恢复读取时拿到的是最新状态，中间缺口由
            # replay_plan/resync 兜底（前端本就按 mid 归并，重复无害）。
            try:
                q.put_nowait(item)
            except Full:
                try:
                    q.get_nowait()      # 丢最旧
                    q.put_nowait(item)  # 再放最新的
                    dropped += 1
                except (Empty, Full):
                    pass  # 与消费者竞态：队列刚被取空/又满，跳过这一条即可
        if dropped:
            # 不静默，但【按时间降频】：一个卡住的客户端会持续丢事件（逐 token
            # 发布时每来一条就丢一条），逐条打警告会把日志刷爆（实测 3 万条
            # 事件刷出上万行），反而把真正的信号埋掉。5 秒最多一条，附累计数。
            now = time.monotonic()
            if now - self._drop_warned_at >= DROP_WARN_INTERVAL_S:
                self._drop_warned_at = now
                log.warning("订阅者队列已满，正在丢弃最旧事件（客户端读取过慢，"
                            "累计已丢 %d 条）——其缺口将由 resync 补齐",
                            self._dropped_total + dropped)
            self._dropped_total += dropped
        if should_persist:
            try:
                self._persist(seq)
            except Exception as e:  # 落库失败不影响事件流
                log.warning("last_seq 落库失败（seq=%s）：%s", seq, e)
        return seq

    # ---------- 订阅 ----------

    def subscribe(self) -> Queue:
        """注册一个订阅者（一条 events 连接），返回它的专属队列。

        队列有容量上限（SUBSCRIBER_QUEUE_MAX），满了由 publish 丢最旧的——
        详见该常量的说明。这里只负责建队列，丢弃策略在发布侧统一实现。
        """
        q: Queue = Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
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
            self._round_started = None
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
