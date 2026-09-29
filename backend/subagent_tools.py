"""
子代理工具（spawn_subagent）——读侧扇出的最小闭环
====================================================

定位（2026-09-29 多 agent 调研定论）：完整的 supervisor/swarm 协作架构不做，
只做「读侧子代理扇出」——大范围探索/检索/交叉核对派出独立上下文的只读子代理，
只把结论回收进主循环，写操作永远留在主代理。收益是上下文隔离：子代理十几轮
的中间读取全部留在它自己的历史里，主代理只为结论付出几百 token（Anthropic
的多代理数据：90%+ 收益来自读侧并行广度，写侧并行只有冲突）。

模块只含 schema / 注册表 / 只读标记与一个薄工具函数；子代理的构造与驱动在
agent.py（Agent._spawn_subagent），经 ToolContext.subagent_runner 注入——
工具层拿不到 Agent 实例，也不 import agent（agent → tools → 本模块是单向
依赖，反向 import 会成环）。本地 _err 与 code_tools._err 同理：失败信封
不向 tools.py 借，避免环。

v0 边界（路线图设计笔记）：
  * 子代理只读：工具面锁死在 SUBAGENT_TOOLSET；spawn_subagent 不在其中——
    递归派生在 schema（模型看不见）与执行（_run_tool 名单把关）两处被挡；
  * 串行：read_only=False，同一轮的多个派生按序跑（并行扇出留 v1）；
  * 事件不外发：子代理过程不进 SSE，父回合时间线只见一次 tool_call /
    tool_result（嵌套 trace 的 parent 标识留 v1，契约见 docs/protocol.md）。
"""

import json

# 子代理可用工具：只读且低风险的侦察面。run_bash / write_file / apply_patch /
# browser_* / analyze_image / create_doc 一律不给——子代理不写工作区、不起
# 浏览器、不出网。todo_write 也不给：清单是主代理的进度工具，子代理的"进度"
# 就是它唯一那条结论。
SUBAGENT_TOOLSET = ("read_file", "list_dir", "grep",
                    "list_attachments", "read_attachment")

# 结论报告长度上限（字符）：上下文隔离的全部意义在于"主代理只付结论的钱"，
# 一份 60k 的报告等于把子代理的上下文又搬回主代理。超限截断并提示派更窄的
# 子任务（60k 的硬闸在 agent._backfill_tool_result，这里是刻意更紧的软闸）。
SUBAGENT_REPORT_MAX_CHARS = 12_000


def _err(msg: str, hint: str = "") -> str:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


def spawn_subagent(task: str = "", ctx=None) -> str:
    """派出一个只读侦察子代理，同步跑完，回收最终结论。

    task 必须自包含：子代理看不到主对话的任何历史，只看得到这一段描述——
    目标（要回答什么）/ 期望输出（结论里要有什么）/ 边界（哪些目录、不要
    做什么）缺一样，侦察就会跑偏。运行器由 agent.py 装配（见模块注释）；
    未装配（理论上不会发生——Agent 构造时自装配）按配置错误拒绝。
    """
    task = str(task or "").strip()
    if not task:
        return _err("task 不能为空",
                    "子代理看不到主对话历史，task 必须自包含：目标/期望输出/边界")
    runner = getattr(ctx, "subagent_runner", None) if ctx is not None else None
    if runner is None:
        return _err("子代理运行器未注入（系统内部配置问题）")
    try:
        return runner(task)
    except Exception as e:  # 运行器内部已兜一层；这里守住"工具绝不抛"的契约
        return _err(f"子代理执行失败: {type(e).__name__}: {e}",
                    "可缩小任务范围重试，或主代理自行侦察")


SUBAGENT_TOOL_REGISTRY = {
    "spawn_subagent": spawn_subagent,
}

SUBAGENT_TOOL_READ_ONLY = {
    # 刻意的 False：子代理一跑十几轮、数分钟，串行保证同一轮派多个时按序
    # 执行、结果可预期；并行扇出（取最慢者的收益）留 v1，前提是先确认
    # llm 客户端多请求并发安全。
    "spawn_subagent": False,
}

SUBAGENT_TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": "派出一个只读侦察子代理，在独立上下文里替你完成大范围探索/检索/"
                       "交叉核对，跑完后只把最终结论回收给你（它的中间读取不占用你的上下文）。"
                       "适用：要读很多文件才能回答的定位问题、全库模式调查、多文件交叉核对；"
                       "一两次 read_file/grep 就能答的简单问题不要派，直接自己查更快。"
                       "task 必须自包含（子代理看不到主对话历史），写清三件事："
                       "目标（要回答什么问题）/ 期望输出（结论里要有什么）/ "
                       "边界（限定哪些目录或文件、不要做什么）。"
                       "子代理没有写权限：它只侦察，改文件/跑命令仍由你自己决定。"
                       "示例：{\"task\": \"目标：找出 workspace 里所有裸 except 的位置；"
                       "边界：只查 *.py；期望输出：文件:行号 列表\"}。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "侦察任务描述（自包含：目标/期望输出/边界）",
                },
            },
            "required": ["task"],
        },
    },
}]
