"""任务工作区解析
==============

解析一个任务的工作区，优先级：任务自选 → 用户默认（仅存量兼容）。逐字搬移。
"""

from pathlib import Path

import db
from code_tools import prepare_workspace


def selected_directory(path: str) -> Path:
    if not path:
        raise ValueError("请选择工作目录")
    target = Path(path).expanduser().resolve()
    if not target.is_dir() or target == Path(target.root):
        raise ValueError("工作目录不存在或不可用")
    return target


def project_directory(user_id: int, sid: str) -> Path | None:
    """读取会话的项目路径，兼容旧用户默认目录。无绑定时返回 None。"""
    if not sid:
        return None
    ws = db.get_session_workspace(sid) or db.get_setting(f"default_workspace:{user_id}")
    return Path(ws).expanduser().resolve() if ws else None


def _resolve_workspace(user_id: int, sid: str) -> Path | None:
    directory = project_directory(user_id, sid)
    return prepare_workspace(directory) if directory else None
