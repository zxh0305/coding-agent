"""稳定性指标聚合（纯函数，无 IO，便于单测）。

为什么单独一层
--------------
`db.usage_summary` 只能回答「花了好多 token」，因为 `message_usage` 里只有
**成功**回合——失败的、被用户停掉的回合根本不进那张表。而「用起来稳不稳」
恰恰要看失败与停止的占比。所以指标的输入是 `diagnostics` 的 rollup 行
（每回合一行，无论结局），本模块只做纯计算。

口径约定（改之前先想清楚，别让两次统计前后矛盾）
------------------------------------------------
- 回合：一行 rollup = 一个用户回合（不是 LLM 请求数）。
- 成功率：outcome == "done" / 全部回合。用户主动停止（stopped）**不算失败**，
  单独计数——把它算进失败会让人误以为系统在崩。
- 首 token：取回合内所有 `llm_first.ttft_s` 的最小值作为该回合的首响；
  没有（例如本轮一次也没吐字就失败）的不参与分位计算。
- 分位统一用最近秩法（nearest-rank），样本少时也不做插值，避免给出
  一个"不存在的"耗时。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def _percentile(values: list[float], p: float) -> Optional[float]:
    """最近秩法分位；空样本返回 None（调用方渲染成「—」）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # nearest-rank：ceil(p/100 * N) - 1
    import math
    idx = max(0, min(len(ordered) - 1, math.ceil(p / 100.0 * len(ordered)) - 1))
    return ordered[idx]


def _round(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(v, nd)


def aggregate(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把 rollup 行聚合成分层指标。

    返回结构（前端用量页直接消费，字段缺失一律给 0/None，避免前端判空爆炸）：
        {
          "turns": 总回合数,
          "by_outcome": {"done": n, "error": n, "stopped": n},
          "success_rate": 0~1,
          "error_rate": 0~1,
          "stop_rate": 0~1,
          "retryable_errors": n,          # 值得让用户点重试的错误数
          "duration_s": {"p50": .., "p95": .., "max": ..},
          "ttft_s": {"p50": .., "p95": ..},
          "rounds_per_turn": {"avg": .., "p95": ..},
          "tool_time_share": 0~1,         # 工具耗时占回合总耗时（衡量是不是在等工具）
        }
    """
    rows = list(rows or [])
    turns = len(rows)
    by_outcome = {"done": 0, "error": 0, "stopped": 0}
    retryable = 0
    durations: list[float] = []
    ttfts: list[float] = []
    rounds_list: list[float] = []
    tool_total = 0.0
    dur_total = 0.0

    for r in rows:
        outcome = str(r.get("outcome") or "done")
        if outcome == "stopped" or r.get("stopped"):
            outcome = "stopped"
        elif outcome not in ("done", "error"):
            outcome = "error"
        by_outcome[outcome] += 1
        if outcome == "error" and r.get("retryable"):
            retryable += 1

        dur = r.get("duration_s")
        if isinstance(dur, (int, float)) and dur >= 0:
            durations.append(float(dur))
            dur_total += float(dur)
        # 首响：优先用明字段；rollup 里若带了就用，否则跳过
        ttft = r.get("ttft_s")
        if isinstance(ttft, (int, float)) and ttft >= 0:
            ttfts.append(float(ttft))
        rn = r.get("rounds")
        if isinstance(rn, (int, float)) and rn > 0:
            rounds_list.append(float(rn))
        tt = r.get("tool_total_s")
        if isinstance(tt, (int, float)) and tt >= 0:
            tool_total += float(tt)

    denom = turns or 1
    return {
        "turns": turns,
        "by_outcome": by_outcome,
        "success_rate": round(by_outcome["done"] / denom, 4),
        "error_rate": round(by_outcome["error"] / denom, 4),
        "stop_rate": round(by_outcome["stopped"] / denom, 4),
        "retryable_errors": retryable,
        "duration_s": {
            "p50": _round(_percentile(durations, 50)),
            "p95": _round(_percentile(durations, 95)),
            "max": _round(max(durations) if durations else None),
        },
        "ttft_s": {
            "p50": _round(_percentile(ttfts, 50), 3),
            "p95": _round(_percentile(ttfts, 95), 3),
        },
        "rounds_per_turn": {
            "avg": _round(sum(rounds_list) / len(rounds_list), 1) if rounds_list else None,
            "p95": _round(_percentile(rounds_list, 95), 1),
        },
        # 工具耗时占比 >0.6 通常说明「慢在命令」而不是「慢在模型」，
        # 这直接对应改造方案里的归因判断
        "tool_time_share": (round(tool_total / dur_total, 3) if dur_total > 0 else None),
    }
