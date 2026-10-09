"""进程内共享状态（模块级可变单例的唯一归宿）
============================================

从原 app.py 抽出：Agent 实例缓存、每会话锁、每会话事件总线、上下文统计、
回合结局标记等**只存内存、进程重启即归零**的状态集中在这里。

为什么要单独一个模块：路由层（routes/）与装配层（app.py）都要读写同一批
dict。放这里后两边 `from state import _agents, _running_agents, ...` 拿到的是
【同一个对象】，语义与它们原先同处 app.py 时完全一致；测试里
`app._running_agents["sX"] = fresh` / `app._running_agents.clear()` 这类"改
容器内容"的写法也照旧生效（app.py 会把这些名字重新导出）。

依赖纪律：本模块【不依赖任何其它后端模块】，仅用标准库。
"""

import threading

from agent import Agent
from events import SessionEvents

# Agent 实例缓存：sid -> (Agent, sig)。sig 是"激活供应商+模型+密钥"的指纹，
# 指纹变了（用户切换模型/改配置）就重建实例，但历史从数据库恢复，不丢对话。
_agents: dict[str, Agent] = {}
_sigs: dict[str, str] = {}
# 正在跑回合的 Agent 实例（sid → agent）。与 _agents 分开记：切模型/切工作区
# 会重建 _agents[sid]，但旧回合仍持【旧】实例在跑——停止请求若只查 _agents，
# 会把开关置到没人听的新实例上（实测：停止后干等 60s 直到旧请求超时才收场）。
_running_agents: dict[str, Agent] = {}
_ctx: dict[str, dict] = {}  # sid -> 最近一次上下文统计（非关键数据，只存内存）
# 最近一轮的结局：sid -> {"outcome": "done"|"error", "seen": bool}（只存内存）。
# 语义是"未读标记"：回合跑完时若用户不在这个会话里，就置 seen=False，列表亮
# 绿点（done）/红点（error）提示"有新结果"；用户切进去看过即置 seen=True，
# 徽标消失。用户正看着的会话前台跑完，会被前端立即标记已读，因此不亮。
# 不落库：进程重启后没有"上一轮"可言，退回无状态是正确的。
_turn_status: dict[str, dict] = {}
_lock = threading.Lock()    # 全局锁：只保护上面的共享 dict 和模型配置的短临界区
_session_locks: dict[str, threading.Lock] = {}  # 每个任务一把锁（见 app._session_lock）

# 每会话一条事件总线（常驻事件流的真相源）。惰性创建：seq 从 sessions.last_seq
# 恢复（重启不归零），persist 回调把最新 seq 写回去。删除任务时整体摘除。
_buses: dict[str, SessionEvents] = {}
