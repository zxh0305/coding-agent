"""
日志配置
=========

标准库 logging 的"双通道"输出，这是生产项目的标准做法：

  * data/logs/agent.log —— 当天的日志（DEBUG 级全量记录：用户的每次提问、每轮
    发给 LLM 的完整 payload、LLM 原始返回、工具调用与结果、错误堆栈）。
    每天零点自动切分：昨天的变成 data/logs/agent.log.2026-09-19 这样的日期文件，
    默认保留 14 天（LOG_KEEP_DAYS 可调），过期的自动清理。
  * 终端 —— Web 版运行时实时滚动 INFO 级关键事件（提问/工具调用/回答），
    看起来是"实时"的；命令行版本身就有彩色打印，关闭终端通道避免重复。

要点：print 和 logging 的分工 —— print 是"给人看的界面"，
logging 是"给机器/给自己查的档案"。tail -f data/logs/agent.log 可实时追看。
"""

import contextvars
import logging
import os
import shutil
import time
from contextlib import contextmanager
from logging.handlers import TimedRotatingFileHandler

# 日志文件夹固定在项目根目录的 data/ 下（logger.py 位于 backend/ 下，往上跳一级），
# 与数据库、artifacts 同址，不受从哪个目录启动命令影响
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# ── 结构化日志上下文 ────────────────────────────────────────────────
# 让一行日志自带「哪个会话 / 哪一轮 / 哪个工具」，出事时不用靠时间戳猜现场。
# 默认关闭（LOG_CONTEXT 未设置时输出与旧版逐字符一致），设置 LOG_CONTEXT=1 才追加前缀：
#     2026-11-18 16:25:00 [INFO] [sid=abc12345 turn=3 tool=read_file] ...
# 用 contextvars 而非全局变量：工具可能在线程/任务里跑，上下文要跟着执行流走。
_sid_var: contextvars.ContextVar[str] = contextvars.ContextVar("log_sid", default="")
_turn_var: contextvars.ContextVar[str] = contextvars.ContextVar("log_turn", default="")
_tool_var: contextvars.ContextVar[str] = contextvars.ContextVar("log_tool", default="")


def _context_enabled() -> bool:
    return os.environ.get("LOG_CONTEXT", "").strip().lower() in ("1", "true", "yes", "on")


def set_log_context(*, sid=None, turn=None, tool=None) -> None:
    """设置日志上下文；传 None 的字段保持不变（便于只覆盖一层）。"""
    if sid is not None:
        _sid_var.set(str(sid)[:8])
    if turn is not None:
        _turn_var.set(str(turn))
    if tool is not None:
        _tool_var.set(str(tool)[:32])


def clear_log_context() -> None:
    _sid_var.set("")
    _turn_var.set("")
    _tool_var.set("")


@contextmanager
def tool_context(name: str):
    """只临时设置工具名的便捷入口（工具执行前后包一层）。

    与 log_context 同样基于 token 还原：本方法可能跑在线程池里、嵌套在
    子代理的回合上下文中，清空会产生错误的归属。
    """
    token = _tool_var.set(str(name)[:32])
    try:
        yield
    finally:
        _tool_var.reset(token)


@contextmanager
def log_context(*, sid=None, turn=None, tool=None):
    """临时叠加一层上下文，退出时还原到进入前的值（支持嵌套）。"""
    tokens = []
    if sid is not None:
        tokens.append((_sid_var, _sid_var.set(str(sid)[:8])))
    if turn is not None:
        tokens.append((_turn_var, _turn_var.set(str(turn))))
    if tool is not None:
        tokens.append((_tool_var, _tool_var.set(str(tool)[:32])))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class _ContextFormatter(logging.Formatter):
    """占位符已设置时，在级别标签后插入 [sid=… turn=… tool=…]。

    关闭时（LOG_CONTEXT 未开）直接返回父类结果，零行为变化。
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not _context_enabled():
            return text
        parts = []
        sid, turn, tool = _sid_var.get(), _turn_var.get(), _tool_var.get()
        if sid:
            parts.append(f"sid={sid}")
        if turn:
            parts.append(f"turn={turn}")
        if tool:
            parts.append(f"tool={tool}")
        if not parts:
            return text
        level_tag = f"[{record.levelname}]"
        idx = text.find(level_tag)
        if idx < 0:
            return f"[{' '.join(parts)}] " + text
        cut = idx + len(level_tag)
        return text[:cut] + f" [{' '.join(parts)}]" + text[cut:]


def prune_old_logs(log_dir: str, keep_days: int, max_total_mb: float = 200.0) -> int:
    """启动时主动回收过期日志，返回删除的文件数。

    为什么需要它：TimedRotatingFileHandler 的 backupCount 只在「跨零点切分」
    那一刻，对已被重命名的 agent.log.* 做删除，且只按【条数】比较——目录里
    的历史文件数不足 backupCount 时一个都不会删。后果就是单个暴涨的日志
    （曾观测到一天 367MB）会永久残留在 data/logs 下。此外进程若长期不在零点
    前后运行，跨天切分从不发生，清理逻辑根本不执行。

    这里补一道与清理 browser-profiles 同思路的启动兜底：
      * 按 mtime 删除超过 keep_days 的 agent.log.*；
      * 若剩余日志总量仍超 max_total_mb，从最旧的开始继续删，直到压回限额
        （但永不删今天正在写的 agent.log 本体）。
    keep_days<=0 表示关闭。单文件删除失败不阻断其余。
    """
    if keep_days <= 0 or not os.path.isdir(log_dir):
        return 0
    current = os.path.join(log_dir, "agent.log")
    backups = sorted(
        (os.path.join(log_dir, f) for f in os.listdir(log_dir)
         if f.startswith("agent.log.") and f != "agent.log"),
        key=lambda p: os.path.getmtime(p),
    )
    removed = 0
    deadline = time.time() - keep_days * 86400
    survivors: list[str] = []
    for p in backups:
        try:
            if os.path.getmtime(p) < deadline:
                os.remove(p)
                removed += 1
            else:
                survivors.append(p)
        except OSError:
            continue
    # 总量兜底：仍在的备份（+ 当天文件）合计超限就从最旧的删起
    limit = max_total_mb * 1024 * 1024
    def _size(paths):
        total = 0
        for p in paths:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total
    while survivors and _size(survivors + [current]) > limit:
        oldest = survivors.pop(0)
        try:
            os.remove(oldest)
            removed += 1
        except OSError:
            continue
    return removed


def setup_logging(console: bool = False) -> str:
    """初始化日志，返回当天日志文件路径。

    文件夹可用 .env 的 LOG_DIR 覆盖；保留天数 LOG_KEEP_DAYS（默认 14，过期自动删）；
    文件级别 LOG_LEVEL（默认 DEBUG 全量）。console=True 时终端同步输出
    INFO 级以上事件（Web 版用）。
    """
    log_dir = os.environ.get("LOG_DIR") or os.path.join(_PROJECT_DIR, "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "agent.log")

    # 兼容迁移：旧版把日志放在项目根目录，首次升级时整个挪进日志文件夹，
    # 历史记录不丢。必须在创建 FileHandler 之前做（否则写进旧 inode）。
    legacy = os.path.join(_PROJECT_DIR, "agent.log")
    if os.path.exists(legacy) and not os.path.exists(log_file):
        shutil.move(legacy, log_file)

    root = logging.getLogger("")
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return log_file  # 已初始化过（例如测试中多次调用），不重复挂 handler

    root.setLevel("DEBUG")
    keep_days = int(os.environ.get("LOG_KEEP_DAYS", "14"))
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",        # 每天零点切分
        backupCount=keep_days,  # 只保留最近 N 天，过期的自动删除
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"  # 轮转后的文件名：agent.log.2026-09-20
    file_handler.setLevel(os.environ.get("LOG_LEVEL", "DEBUG").upper())
    file_handler.setFormatter(_ContextFormatter(_FMT, _DATEFMT))
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel("INFO")  # 终端只要关键事件，DEBUG 全量留给文件
        console_handler.setFormatter(_ContextFormatter(_FMT, _DATEFMT))
        root.addHandler(console_handler)
    return log_file
