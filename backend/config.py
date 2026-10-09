"""路径常量（后端各模块共用的"项目根从哪来"底座）
================================================

从原 app.py 顶部抽出：所有路径都基于本文件位置反推，与进程的工作目录（CWD）
无关——这样无论从哪里 `python3 backend/app.py` / `python3 -m ...` 启动，目录
都不会跑偏（历史上有 4 处 `Path("data/...")` 相对路径依赖 CWD 的隐患，本层
统一为绝对路径口径）。

依赖纪律：本模块【不依赖任何其它后端模块】，谁都可以 import 它。
"""

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
ENV_FILE = PROJECT_DIR / ".env"
