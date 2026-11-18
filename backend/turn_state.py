"""回合状态机（首版：只断言告警，不拦截执行）。

为什么要有它
------------
`Agent._run` 用一堆布尔量（`_completion_final` / `_round_had_progress` /
`_final_candidate` …）和 6 个 return/break 推进一轮对话。所有"不能这么走"
的约束目前都写在注释里，靠人记住，例如：

    # 不能把带 tool_calls 的悬空 assistant 消息留在历史里（下一轮会 400）

注释不是约束，机器才是。ZCode 的做法是 `canTransitionTo` 直接拒绝非法迁移；
这里先做同等表达、但**更保守的第一步**：记录并断言，发现问题只告警不中断。

为什么不直接 fail-fast
----------------------
回合循环里存在"合法但看起来奇怪"的路径（上下文压缩后重来、收尾轮复用同一
次流、工具被拒后重规划）。第一版就拦，会把正常运行打断在用户面前。所以：
  · AGENT_STATE_STRICT=1 才抛错；默认只计一条告警到诊断/日志。
  · 告警持续为零一段时间后，再把默认切到 strict。

状态语义
--------
    IDLE         回合开始前
    REQUESTING   已组装请求、正在等首个增量（最长的等待期之一）
    STREAMING    正在接收增量
    TOOL_RUNNING 正在执行工具
    WRAPPING_UP  收尾轮（不再给工具，只出最终回答）
    CANCELLING   用户 / 上层要求停止后的退场路径
    DONE / ERROR 终态

迁移刻意放得比较宽（例如 STREAMING→TOOL_RUNNING 之外，也允许
TOOL_RUNNING→STREAMING 的多工具连续执行），因为约束的目的是抓"越界"，
而不是给实现添堵。
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("turn_state")

IDLE = "idle"
REQUESTING = "requesting"
STREAMING = "streaming"
TOOL_RUNNING = "tool_running"
WRAPPING_UP = "wrapping_up"
CANCELLING = "cancelling"
DONE = "done"
ERROR = "error"

TERMINAL = frozenset({DONE, ERROR})

# 允许的迁移：from → 可去往的状态集合
# 注意 STREAMING→STREAMING（收尾轮复用同一次流的增量仍在到达）是有意的。
_ALLOWED: dict[str, frozenset[str]] = {
    IDLE:         frozenset({REQUESTING, CANCELLING, DONE, ERROR}),
    REQUESTING:   frozenset({STREAMING, CANCELLING, ERROR, DONE}),
    STREAMING:    frozenset({STREAMING, TOOL_RUNNING, REQUESTING, WRAPPING_UP,
                             CANCELLING, DONE, ERROR}),
    TOOL_RUNNING: frozenset({STREAMING, TOOL_RUNNING, REQUESTING, WRAPPING_UP,
                             CANCELLING, DONE, ERROR}),
    WRAPPING_UP:  frozenset({STREAMING, WRAPPING_UP, CANCELLING, DONE, ERROR}),
    CANCELLING:   frozenset({DONE, ERROR}),
    DONE:         frozenset(),   # 终态：想再开始必须新建一个状态对象
    ERROR:        frozenset(),
}


def strict_mode() -> bool:
    return os.environ.get("AGENT_STATE_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


class IllegalTransition(RuntimeError):
    """非法迁移。仅在 strict 模式下抛出。"""


class TurnState:
    """单个回合的状态；线程安全（回合线程与看护线程可能同时读）。"""

    __slots__ = ("_state", "_lock", "_history", "_warned")

    def __init__(self, initial: str = IDLE):
        self._state = initial
        self._lock = threading.Lock()
        self._history: list[tuple[str, str]] = []
        self._warned: list[tuple[str, str]] = []

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def can_transition(self, target: str) -> bool:
        with self._lock:
            return target in _ALLOWED.get(self._state, frozenset())

    def to(self, target: str, note: str = "") -> bool:
        """尝试迁移。

        合法 → 迁移并返回 True。
        非法 → 记录一条告警；strict 模式抛 IllegalTransition，否则停留在原状态并返回 False。
        """
        with self._lock:
            src = self._state
            if target not in _ALLOWED.get(src, frozenset()):
                self._warned.append((src, target))
                message = f"非法状态迁移 {src} → {target}" + (f"（{note}）" if note else "")
                if strict_mode():
                    raise IllegalTransition(message)
                log.warning("%s —— 已忽略（AGENT_STATE_STRICT=1 会让它变成错误）", message)
                return False
            self._state = target
            self._history.append((src, target))
            return True

    def illegal_count(self) -> int:
        with self._lock:
            return len(self._warned)

    def history(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._history)
