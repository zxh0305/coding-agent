"""路由层的公共引用点（注入 + 底层再导出）
========================================

routes/* 里除了各 mixin，还需要几样"跨域共用"的东西：

  1. 底层模块的再导出（db / state 的字典 / 业务服务函数）——让各 mixin 只
     `from routes import busref` 一处，避免每个文件重复一长串 import；
  2. 装配层运行时常量（log、会话锁、会话总线工厂、会话状态计算、权限闸门、
     停止目标、记忆提取启动点）——这些定义在 app.py / 装配层，由
     app.install_service_hooks() 在导入后注入，**绝不能在 import 期取值**
     （那会形成 routes ↔ app 循环依赖）。

使用方式：模块内以属性方式访问（busref.log、busref._event_bus），这样注入发生在
import 之后也能生效。
"""

import logging

import db
from state import (_agents, _buses, _ctx, _lock, _running_agents, _session_locks,
                   _sigs, _turn_status)

# 与 app 同名同源的日志器（不 import app，避免循环依赖）
log = logging.getLogger("app")

# ---- 以下由 app.install_service_hooks() 注入（import 后赋值）----
_session_lock = None          # 按会话加锁
_event_bus = None             # 会话事件总线工厂
_session_state = None         # 任务运行状态计算
_build_permission_gate = None  # 权限闸门构造
_stop_target = None           # 停止请求目标解析
_spawn_memory_extraction = None  # 轮末记忆提取启动点
_run_round = None             # 回合执行体（services.turn._run_round）

# 本模块声明的全部注入点。install() 与 assert_installed() 都以它为唯一事实
# 来源——加新注入点只需在这里补一个名字（下面的 None 占位仍要写，供类型提示
# 与读者对照），校验自动跟上，不会漏。
INJECTION_POINTS: tuple[str, ...] = (
    "_session_lock", "_event_bus", "_session_state", "_build_permission_gate",
    "_stop_target", "_spawn_memory_extraction", "_run_round",
)


def install(**kwargs) -> None:
    """由 app 在装配完成后调用，把装配层能力注入本模块。"""
    g = globals()
    for k, v in kwargs.items():
        if k not in g:
            raise KeyError(f"busref 无此注入点：{k}")
        g[k] = v


def assert_installed() -> None:
    """校验本模块声明的注入点【全部】已被赋值，缺失即抛错（fail fast）。

    为什么要它：install() 只挡"注入了不存在的名字"，挡不住"声明的名字忘了
    注入"。后者会留下一个恒为 None 的占位，直到某个请求走到那行才炸——真实
    事故：app.py 拆 services/routes 时 dispatch 在 import 期取别名拿到 None，
    GET /api/sessions 每次 500，前端任务栏整条空掉（见 test_session_list.py
    的自述）。把它提前到装配完成时校验，坏的是启动而不是用户的页面。
    """
    missing = [k for k in INJECTION_POINTS if globals().get(k) is None]
    if missing:
        raise RuntimeError(
            "装配层注入不完整，busref 缺少：" + "、".join(missing)
            + "。请检查 app.install_service_hooks() 是否把这些名字传给了 busref.install()。")
