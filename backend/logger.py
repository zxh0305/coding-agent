"""
日志配置
=========

标准库 logging 的"双通道"输出，这是生产项目的标准做法：

  * agent.log —— 给排错/复盘用，DEBUG 级全量记录：用户的每次提问、
    每轮发给 LLM 的完整 payload、LLM 原始返回、工具调用与结果、
    错误堆栈。出了问题先翻日志，再回忆终端上滚走了什么。
  * 终端 —— Web 版运行时实时滚动 INFO 级关键事件（提问/工具调用/回答），
    看起来是"实时"的；命令行版本身就有彩色打印，关闭终端通道避免重复。

要点：print 和 logging 的分工 —— print 是"给人看的界面"，
logging 是"给机器/给自己查的档案"，二者级别、格式、去向互不干扰。
每条日志写完都会立即 flush 落盘，tail -f agent.log 可以实时追看。
"""

import logging
import os

# 日志文件固定放在项目根目录（logger.py 位于 backend/ 下，往上跳一级），
# 不受从哪个目录启动命令影响
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(console: bool = False) -> str:
    """初始化日志，返回日志文件路径。

    可在 .env 中用 LOG_FILE / LOG_LEVEL 覆盖文件路径与文件级别（默认 DEBUG 全量）。
    console=True 时终端同步输出 INFO 级以上事件（Web 版用）。
    """
    log_file = os.environ.get("LOG_FILE") or os.path.join(_PROJECT_DIR, "agent.log")
    level = os.environ.get("LOG_LEVEL", "DEBUG").upper()

    root = logging.getLogger("")
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return log_file  # 已初始化过（例如测试中多次调用），不重复挂 handler

    root.setLevel("DEBUG")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel("INFO")  # 终端只要关键事件，DEBUG 全量留给文件
        console_handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        root.addHandler(console_handler)
    return log_file
