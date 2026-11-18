"""回合诊断轨迹（结构化、只记边界事件）。

为什么需要它
------------
线上反馈「卡住了」，我们能拿到的只有日志时间戳，无法回答三个最基本的问题：
  · 这一回合到底跑了几轮、每轮多久？
  · 是哪一次工具调用慢，还是模型迟迟不吐字？
  · 结局是正常、出错、还是用户停止？
`data/logs/` 里的日志是给人读的散文，且没有可机读的字段；`session_traces`
是为回放界面服务的（含正文），体积大且不追求「一眼看出慢在哪」。

所以单独做一份**只记边界事件**的诊断轨迹：轮开始/结束、模型请求/首 token、
工具开始/结束、终态。不记录任何正文、参数、工具输出，体积可控，适合长期留存。

设计约束（有意为之）
--------------------
1. **绝不阻塞回合**：所有写盘都包在 try 里，失败只记日志。诊断是观测面，
   不能因为磁盘满/权限问题把一次成功的回合变成失败。
2. **不引入新依赖**：JSONL 追加写，一行一条，出问题时可 `grep` 直读。
3. **可随时关闭**：DIAGNOSTICS=0 关闭（默认开启，但只在回合收尾写一次汇总，
   事件量极低）。
4. **不写正文**：只写结构化字段，避免把用户代码/隐私二次落盘。
5. 保留策略：每个会话最多保留最近 N 个回合文件，超出按修改时间清理。

文件布局
--------
    data/diagnostics/<sid>/<turn_started_at>_<user_mid[:8]>.jsonl

一行一个 JSON 对象，字段固定为：
    ts       Unix 时间戳（秒，带小数）
    kind     事件类型（见下）
    round    轮号（1 起；回合级事件为 0）
    extra    该事件的附加字段（工具名、耗时、耗时归因等）

kind 取值
---------
    turn_start   回合开始（extra: model_input_chars）
    round_start  轮开始（extra: round）
    llm_first    本轮首个增量到达（extra: ttft_s）
    round_end    轮结束（extra: round_s）
    tool_start   工具开始（extra: tool）
    tool_end     工具结束（extra: tool, ok, duration_ms）
    turn_end     回合终态（extra: outcome, rounds, duration_s, tool_total_s,
                             llm_total_s, error_class, retryable, stopped）
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("diagnostics")

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_DIAG_DIR = _PROJECT_DIR / "data" / "diagnostics"

# 每个会话保留的回合文件数；诊断只需要覆盖"最近出问题的那几次"。
KEEP_PER_SESSION = 30


def enabled() -> bool:
    """DIAGNOSTICS=0 / off / false 时关闭；默认开启。"""
    v = os.environ.get("DIAGNOSTICS", "").strip().lower()
    return v not in ("0", "off", "false", "no")


def _session_dir(sid: str) -> Path:
    # sid 来自数据库（uuid hex 前缀），仍然是用户可控的字符串；
    # 只保留字母数字与短横线，杜绝 ../ 之类路径穿越
    safe = "".join(c for c in str(sid) if c.isalnum() or c in "-_")[:40] or "unknown"
    return _DIAG_DIR / safe


def turn_path(sid: str, started_at: float, user_mid: str) -> Path:
    mid = "".join(c for c in str(user_mid or "") if c.isalnum())[:8] or "nomid"
    return _session_dir(sid) / f"{int(started_at)}_{mid}.jsonl"


# 指标汇总行集中放一个文件：回合明细每会话只留 30 个且会被清理，
# 指标要覆盖「全部回合」，所以单独一份 append-only 的 rollup。
ROLLUP_PATH = _DIAG_DIR / "rollup.jsonl"
# rollup 的保留上限（行数）；超出时整体重写保留最近一半，避免无限增长
ROLLUP_MAX_LINES = 20000


def _append_rollup(sid: str, started_at: float, summary: dict[str, Any]) -> None:
    """追加一行回合汇总。失败只记日志，绝不影响回合。"""
    try:
        ROLLUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": round(started_at, 3), "sid": str(sid), **summary}
        with open(ROLLUP_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        _maybe_trim_rollup()
    except Exception:
        log.exception("[会话 %s] 指标汇总行写入失败（忽略）", sid)


def _maybe_trim_rollup() -> None:
    """行数超限时保留最近一半。只在确实超限时读文件，正常路径无额外 IO。"""
    try:
        if not ROLLUP_PATH.exists():
            return
        count = 0
        with open(ROLLUP_PATH, "r", encoding="utf-8") as fh:
            for _ in fh:
                count += 1
        if count <= ROLLUP_MAX_LINES:
            return
        with open(ROLLUP_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        keep = lines[count // 2:]
        tmp = ROLLUP_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, ROLLUP_PATH)
    except Exception:
        log.exception("指标汇总行裁剪失败（忽略）")


def read_rollup(days: Optional[int] = None) -> list[dict[str, Any]]:
    """读取指标汇总行；days 只保留最近 N 天。坏行跳过（历史文件可能被截断）。"""
    rows: list[dict[str, Any]] = []
    if not ROLLUP_PATH.exists():
        return rows
    cutoff = time.time() - days * 86400 if days else None
    try:
        with open(ROLLUP_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if cutoff is not None and (row.get("ts") or 0) < cutoff:
                    continue
                rows.append(row)
    except Exception:
        log.exception("指标汇总行读取失败（返回已读部分）")
    return rows


class TurnRecorder:
    """一回合的诊断记录器：内存攒事件，收尾时一次落盘。

    为什么攒着而不是边跑边写：回合内事件最多几十条，攒着写只有一次系统调用，
    不会在流式关键路径上引入额外 IO；代价是进程被 kill 时丢失本回合记录——
    可接受，因为崩溃现场由 `data/logs/` 的日志兜底。
    """

    def __init__(self, sid: str, user_mid: str, started_at: Optional[float] = None):
        self.sid = sid
        self.user_mid = user_mid
        self.started_at = started_at if started_at is not None else time.time()
        self._events: list[dict[str, Any]] = []
        self._round_started: Optional[float] = None
        self._llm_first_at: Optional[float] = None
        self._tool_started: dict[str, float] = {}
        self._tool_total = 0.0
        self._llm_total = 0.0
        self._rounds = 0
        # 整回合首响：第一次「轮开始 → 首个增量」的间隔。稳定性的关键指标之一，
        # 与 llm_client 的 ttft_s 不同（那个是单次请求级，这里是用户体感级）。
        self._first_ttft: Optional[float] = None

    # ── 记录 ──────────────────────────────────────────────
    def _add(self, kind: str, round_no: int = 0, **extra: Any) -> None:
        if not enabled():
            return
        self._events.append({"ts": round(time.time(), 3), "kind": kind,
                             "round": round_no, "extra": extra})

    def turn_start(self, model_input_chars: int = 0) -> None:
        self._add("turn_start", model_input_chars=model_input_chars)

    def round_start(self, round_no: int) -> None:
        self._rounds = max(self._rounds, round_no)
        self._round_started = time.time()
        self._llm_first_at = None
        self._add("round_start", round_no, round=round_no)

    def llm_first_token(self, round_no: int) -> None:
        """本轮首个增量到达（首次调用才记，用于 TTFT）。"""
        if self._llm_first_at is not None or self._round_started is None:
            return
        now = time.time()
        self._llm_first_at = now
        delta = now - self._round_started
        if self._first_ttft is None:      # 整回合首响只取第一次
            self._first_ttft = delta
        self._add("llm_first", round_no, ttft_s=round(delta, 3))

    def round_end(self, round_no: int) -> None:
        now = time.time()
        if self._round_started is not None:
            self._llm_total += now - self._round_started
            self._add("round_end", round_no, round_s=round(now - self._round_started, 3))
        self._round_started = None
        self._llm_first_at = None

    def tool_start(self, name: str) -> None:
        # 同名工具并行时用「最后开始的那个」计时：诊断只求量级，不求精确到每次
        self._tool_started[name] = time.time()
        self._add("tool_start", self._rounds, tool=name)

    def tool_end(self, name: str, ok: bool = True) -> None:
        started = self._tool_started.pop(name, None)
        dur_ms = int((time.time() - started) * 1000) if started else -1
        if dur_ms >= 0:
            self._tool_total += dur_ms / 1000.0
        self._add("tool_end", self._rounds, tool=name, ok=bool(ok), duration_ms=dur_ms)

    def finish(self, *, outcome: str, error_class: str = "", retryable: bool = False,
               stopped: bool = False) -> None:
        """写下终态并落盘。outcome: done / error / stopped。"""
        if not enabled():
            return
        # 未结算的工具（被 kill / 异常跳出）补一条，避免诊断里出现"开始无结束"
        now = time.time()
        for name, started in list(self._tool_started.items()):
            self._add("tool_end", self._rounds, tool=name, ok=False,
                      duration_ms=int((now - started) * 1000), unsettled=True)
        self._tool_started.clear()
        summary = {
            "outcome": outcome,
            "rounds": self._rounds,
            "duration_s": round(now - self.started_at, 3),
            "tool_total_s": round(self._tool_total, 3),
            "llm_total_s": round(self._llm_total, 3),
            "error_class": error_class,
            "retryable": bool(retryable),
            "stopped": bool(stopped),
            "ttft_s": (round(self._first_ttft, 3) if self._first_ttft is not None else None),
        }
        self._add("turn_end", self._rounds, **summary)
        self.flush()
        # 汇总行单独进 rollup：稳定性指标要按「全部回合」统计，而回合明细文件
        # 每会话只留 30 个、且会被清理——指标不能依赖明细文件还存在。
        _append_rollup(self.sid, self.started_at, summary)

    # ── 落盘 ──────────────────────────────────────────────

    # ── 落盘 ──────────────────────────────────────────────
    def flush(self) -> None:
        try:
            path = turn_path(self.sid, self.started_at, self.user_mid)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for ev in self._events:
                    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._events = []
            self._prune(path.parent)
        except Exception:
            # 诊断失败绝不能影响回合结果
            log.exception("[会话 %s] 诊断轨迹落盘失败（忽略）", self.sid)

    def _prune(self, directory: Path) -> None:
        try:
            files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            for old in files[:-KEEP_PER_SESSION]:
                old.unlink(missing_ok=True)
        except Exception:
            log.exception("[会话 %s] 诊断轨迹清理失败（忽略）", self.sid)
