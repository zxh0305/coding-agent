"""SSE 事件契约：唯一真相源（单一出处）。

为什么要有它
------------
`docs/protocol.md` 里已经把每个事件的字段写清楚了，但**文档不是可执行的**：
后端某次改动少发一个 `mid`、把 `arguments` 从字符串改成对象，都不会有人
报警——要等到前端某块卡片渲染错位，才发现契约已经漂了。ZCode 的做法是
把这件事交给 `@zcode/contracts` 包，前后端共用一份类型。

本模块把它落成 Python 侧的可执行版本：
  · EVENTS 声明全部事件名 → 必填字段 / 可选字段
  · validate(event) 在开发模式（EVENT_CONTRACT_STRICT=1）下校验
  · schema() 导出给前端做对照（构建脚本生成，不手写第二份）

设计约束
--------
1. **默认不校验**：生产路径上逐事件做 dict 检查会给每个 token 加开销；
   `EVENT_CONTRACT_STRICT=1` 才开。开了以后只 warning 不抛错（除非 STRICT=2），
   避免一个typo把整条 SSE 打挂、用户面前"卡住"。
2. **只声明，不推断**：字段表照抄 docs/protocol.md §4，不做"看起来应该也有"
   的补全。文档和代码冲突时，以文档为准并改这里。
3. **未知事件不报错**：新增事件类型时前后端可能不同版本，未知类型只在
   strict 模式记为 info，不算违约。
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("event_contract")

# 事件名 → (必填字段, 可选字段)
# 顺序与 docs/protocol.md 的事件目录保持一致，便于互相核对。
EVENTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # ── 回合骨架 ──────────────────────────────────────────
    "turn_start": (
        frozenset({"nonce", "input", "started_at"}),
        frozenset({"atts", "user_mid", "model"}),
    ),
    "turn_end": (
        frozenset({"user_mid"}),
        frozenset({"stopped", "outcome"}),
    ),
    "error": (
        frozenset({"message", "retryable"}),
        frozenset({"user_mid", "kind"}),
    ),
    # ── 轮与增量 ──────────────────────────────────────────
    # mid 由 turn.py 发布时补（每轮一个 seg_mid，前端据此开新的过程段）
    "round": (
        frozenset({"round"}),
        frozenset({"mid", "started_at", "final"}),
    ),
    # 注意：增量字段名是 delta（不是 docs 早期写的 text）。turn.py 发布时
    # 由它补 mid（answer_delta 带 seg_mid 归并进同一气泡；reasoning_delta
    # 刻意不带 mid——带了推理文字就会冒充正文）。
    "answer_delta": (
        frozenset({"mid", "delta"}),
        frozenset({"round"}),
    ),
    "reasoning_delta": (
        frozenset({"delta"}),
        frozenset({"round"}),
    ),
    # ── 工具 ──────────────────────────────────────────────
    "tool_call": (
        frozenset({"name", "arguments"}),
        frozenset({"parent", "index", "id"}),
    ),
    "tool_result": (
        frozenset({"name", "result"}),
        frozenset({"parent", "index", "id"}),
    ),
    # ── 终态与旁路 ────────────────────────────────────────
    # done 是前端渲染"最终回答"的载体，字段比骨架事件多得多：
    # answer 是正文、usage/elapsed_s/timing 供状态条与用量、context 系列
    # 刷新容量徽章。全部列为可选：不同终态（正常/停止/超轮数/报错）带的
    # 字段不同，能落地的最小集合是空。
    "done": (
        frozenset(),
        frozenset({"mid", "rounds", "report_head", "error", "stopped_reason",
                   "answer", "elapsed_s", "usage", "timing",
                   "context", "context_tokens", "cache_hit_rate"}),
    ),
    "compacted": (
        frozenset({"summary"}),
        frozenset({"prompt_tokens", "context"}),
    ),
    "api_retry": (
        frozenset({"error", "attempt", "max_attempts", "wait"}),
        frozenset(),
    ),
    # ── 子代理过程（收窄集合：只发不带 delta 的骨架事件）────
    "subagent_start": (
        frozenset(),
        frozenset({"parent", "index", "task"}),
    ),
    "subagent_end": (
        frozenset(),
        frozenset({"parent", "index", "ok"}),
    ),
    # ── 传输层控制（由 events.py 自己产生）─────────────────
    "caught_up": (
        frozenset(),
        frozenset({"running", "seq"}),
    ),
    "resync": (
        frozenset(),
        frozenset({"reason", "latest_seq"}),
    ),
}


def strict_level() -> int:
    """0=不校验（默认）；1=只告警；2=抛错。"""
    v = os.environ.get("EVENT_CONTRACT_STRICT", "").strip()
    if v in ("2", "raise"):
        return 2
    if v in ("1", "true", "yes", "on"):
        return 1
    return 0


class ContractViolation(Exception):
    """事件不符合契约（仅 strict_level()==2 时抛出）。"""


def validate(event: Any) -> list[str]:
    """校验一条事件，返回问题列表（空列表 = 合规）。

    只做"结构"检查，不校验取值类型以外的语义：契约的目的是抓前后端
    **形状**上的漂移，不是替业务逻辑做断言。
    """
    problems: list[str] = []
    if not isinstance(event, dict):
        return [f"事件必须是 dict，实际是 {type(event).__name__}"]
    etype = event.get("type")
    if not isinstance(etype, str) or not etype:
        return ["事件缺少 type 字段"]
    spec = EVENTS.get(etype)
    if spec is None:
        return [f"未知事件类型：{etype}"]   # 调用方决定是 info 还是 warning
    required, optional = spec
    missing = [f for f in required if f not in event]
    if missing:
        problems.append(f"{etype} 缺少必填字段：{', '.join(sorted(missing))}")
    extra = set(event) - required - optional - {"type", "seq", "ts"}
    if extra:
        # 多余字段通常是"改字段时漏改这里"，提示比静默接受更有用
        problems.append(f"{etype} 出现未声明字段：{', '.join(sorted(extra))}")
    return problems


def check(event: Any, where: str = "") -> bool:
    """按 strict_level 处理校验结果。返回是否合规。"""
    level = strict_level()
    if level == 0:
        return True
    problems = validate(event)
    if not problems:
        return True
    loc = f"（{where}）" if where else ""
    real = []
    for p in problems:
        if p.startswith("未知事件类型"):
            # 未知类型不算违约：前后端版本错开是常态（新事件先上线一个端点）。
            log.info("事件契约：%s%s — 若为新增类型请同步 EVENTS", p, loc)
        else:
            real.append(p)
            log.warning("事件契约违约%s：%s", loc, p)
    if not real:
        return True
    if level >= 2:
        raise ContractViolation("; ".join(real))
    return False


def schema() -> dict[str, Any]:
    """导出给前端的对照表（构建脚本据此生成 events.schema.json）。"""
    return {
        "version": 1,
        "events": {
            name: {"required": sorted(req), "optional": sorted(opt)}
            for name, (req, opt) in EVENTS.items()
        },
    }
