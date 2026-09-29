"""
子代理工具（spawn_subagent）——读侧扇出
==========================================

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

并行扇出（v1）：tasks 数组一次派出多个子任务，运行器在线程池里并发驱动。
安全前提已逐项核实（2026-09-29）：llm_client.chat_stream 全部状态是调用内
局部变量，实例上只有 _stream_usage 幂等布尔降级（GIL 下良性竞态）与只读的
on_retry；browser_tools.manager_for 有锁；每个子代理有独立的 ToolContext/
history/权限闸门，只读工具面之间无共享写。子代理自身仍零写入工作区。

事件契约（docs/protocol.md §4.3）：子代理过程不外发，父回合时间线只见一次
tool_call / tool_result（结论信封）；嵌套 trace 的 parent 标识留后续。
"""

import json

# 子代理可用工具：只读且低风险的侦察面。run_bash / write_file / apply_patch /
# browser_* / analyze_image / create_doc 一律不给——子代理不写工作区、不起
# 浏览器、不出网。todo_write 也不给：清单是主代理的进度工具，子代理的"进度"
# 就是它唯一那条结论。
SUBAGENT_TOOLSET = ("read_file", "list_dir", "grep",
                    "list_attachments", "read_attachment")

# 结论报告长度上限（字符，每个子任务各算各的）：上下文隔离的全部意义在于
# "主代理只付结论的钱"，一份 60k 的报告等于把子代理的上下文又搬回主代理。
# 超限截断并提示派更窄的子任务（60k 的硬闸在 agent._backfill_tool_result，
# 这里是刻意更紧的软闸）。
SUBAGENT_REPORT_MAX_CHARS = 12_000

# 单次派出的并行上限。子代理以 LLM 流式请求为主要等待，3 个并发已能覆盖
# "多角度同时调查"的常见形态；再多的任务应该分轮派——每多一个并发就多一份
# 供应商限流/配额压力，收益边际递减。
SUBAGENT_MAX_PARALLEL = 3


def _err(msg: str, hint: str = "") -> str:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


def spawn_subagent(tasks=None, ctx=None) -> str:
    """派出只读侦察子代理（可并行多个），同步跑完，回收结论。

    tasks 是非空字符串数组（每项一个自包含的子任务），≤SUBAGENT_MAX_PARALLEL
    个时并发执行、按提交顺序回填结果。每项必须自包含：子代理看不到主对话的
    任何历史，只看得到这一段描述——目标（要回答什么）/ 期望输出（结论里要
    有什么）/ 边界（哪些目录、不要做什么）缺一样，侦察就会跑偏。运行器由
    agent.py 装配（见模块注释）；未装配按配置错误拒绝。
    """
    # 容错：模型把单个任务写成字符串也能接住（按 [tasks] 归一）
    if isinstance(tasks, str):
        tasks = [tasks]
    if not isinstance(tasks, list) or not tasks:
        return _err("tasks 必须是非空数组",
                    '示例：{"tasks": ["目标：找出裸 except；边界：只查 *.py；'
                    '期望输出：文件:行号 列表"]}')
    clean = [str(t or "").strip() for t in tasks]
    if any(not t for t in clean):
        return _err("tasks 里存在空任务", "每个元素都必须是自包含的任务描述")
    if len(clean) > SUBAGENT_MAX_PARALLEL:
        return _err(f"一次最多并行派出 {SUBAGENT_MAX_PARALLEL} 个子任务（收到 {len(clean)} 个）",
                    "把相关的任务合并描述，或分多轮派出")
    runner = getattr(ctx, "subagent_runner", None) if ctx is not None else None
    if runner is None:
        return _err("子代理运行器未注入（系统内部配置问题）")
    try:
        return runner(clean)
    except Exception as e:  # 运行器内部已兜一层；这里守住"工具绝不抛"的契约
        return _err(f"子代理执行失败: {type(e).__name__}: {e}",
                    "可缩小任务范围重试，或主代理自行侦察")


SUBAGENT_TOOL_REGISTRY = {
    "spawn_subagent": spawn_subagent,
}

SUBAGENT_TOOL_READ_ONLY = {
    # 刻意的 False：本工具一次调用内部自带并发（≤3 个子代理），不需要、也不
    # 应该靠"同轮多个调用并行"的分组调度来扇出——那会让多个子代理调用挤进
    # 只读并行组，与父回合其余只读工具混跑，失败面和事件序都更难推理。
    # 单个调用独占一个串行组，并发被封装在信封之内。
    "spawn_subagent": False,
}

SUBAGENT_TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": "派出只读侦察子代理，在独立上下文里替你完成大范围探索/检索/"
                       "交叉核对，跑完后只把结论回收给你（它们的中间读取不占用你的上下文）。"
                       "tasks 数组一次可派 1~3 个子任务，多角度的调查应一次并行派出；"
                       "一两次 read_file/grep 就能答的简单问题不要派，直接自己查更快。"
                       "每个 task 必须自包含（子代理看不到主对话历史），写清三件事："
                       "目标（要回答什么问题）/ 期望输出（结论里要有什么）/ "
                       "边界（限定哪些目录或文件、不要做什么）。"
                       "子代理没有写权限：它们只侦察，改文件/跑命令仍由你自己决定。"
                       '示例：{"tasks": ["目标：找出 workspace 里所有裸 except 的位置；'
                       '边界：只查 *.py；期望输出：文件:行号 列表"]}',
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "子任务描述数组（1~3 个，每个自包含：目标/期望输出/边界）",
                    "minItems": 1,
                    "maxItems": SUBAGENT_MAX_PARALLEL,
                },
            },
            "required": ["tasks"],
        },
    },
}]
