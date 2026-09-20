"""
日志配置
=========

标准库 logging 的"双通道"输出，这是生产项目的标准做法：

  * logs/agent.log —— 当天的日志（DEBUG 级全量记录：用户的每次提问、每轮
    发给 LLM 的完整 payload、LLM 原始返回、工具调用与结果、错误堆栈）。
    每天零点自动切分：昨天的变成 logs/agent.log.2026-09-19 这样的日期文件，
    默认保留 14 天（LOG_KEEP_DAYS 可调），过期的自动清理。
  * 终端 —— Web 版运行时实时滚动 INFO 级关键事件（提问/工具调用/回答），
    看起来是"实时"的；命令行版本身就有彩色打印，关闭终端通道避免重复。

要点：print 和 logging 的分工 —— print 是"给人看的界面"，
logging 是"给机器/给自己查的档案"。tail -f logs/agent.log 可实时追看。
"""

import logging
import os
import shutil
from logging.handlers import TimedRotatingFileHandler

# 日志文件夹固定在项目根目录下（logger.py 位于 backend/ 下，往上跳一级），
# 不受从哪个目录启动命令影响
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(console: bool = False) -> str:
    """初始化日志，返回当天日志文件路径。

    文件夹可用 .env 的 LOG_DIR 覆盖；保留天数 LOG_KEEP_DAYS（默认 14，过期自动删）；
    文件级别 LOG_LEVEL（默认 DEBUG 全量）。console=True 时终端同步输出
    INFO 级以上事件（Web 版用）。
    """
    log_dir = os.environ.get("LOG_DIR") or os.path.join(_PROJECT_DIR, "logs")
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
    file_handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel("INFO")  # 终端只要关键事件，DEBUG 全量留给文件
        console_handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        root.addHandler(console_handler)
    return log_file
