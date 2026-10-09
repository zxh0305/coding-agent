"""任务工作区解析
==============

解析一个任务的工作区，优先级：任务自选 → 用户默认（仅存量兼容）。逐字搬移。
"""

from pathlib import Path

import db
from code_tools import prepare_workspace


def _resolve_workspace(user_id: int, sid: str) -> Path | None:
    """解析一个任务的工作区，优先级：任务自选 → 用户默认（仅存量兼容）。

    返回 None = 任务未绑定项目（新任务未选目录时）：工具调用会被闸门拦下，
    前端引导先选目录。sid 为空时返回 None（新任务的绑定发生在选目录那一刻，
    不再预绑用户默认——此前默认目录会让"新任务"悄悄带上项目归属）。
    """
    ws = db.get_session_workspace(sid) if sid else None
    if not ws:
        # 存量兼容：老会话（升级前就有 workspace 行为空）落回用户默认，
        # 不至于让历史任务突然失去项目归属；新任务不走这条路。
        legacy = db.get_setting(f"default_workspace:{user_id}") if sid else None
        return prepare_workspace(legacy) if legacy else None
    return prepare_workspace(ws)
