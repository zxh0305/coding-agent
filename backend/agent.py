"""
Agent 核心循环 —— 本项目最值得精读的文件
==========================================

抛开各种框架和名词，一个 Agent 的本质就是一个 while 循环：

    ┌─────────────────────────────────────────────────┐
    │ 1. 把【系统提示 + 完整对话历史 + 工具清单】发给 LLM   │
    │ 2. LLM 返回一条 message：                          │
    │      a. 带文字 → 这就是最终回答，结束               │
    │      b. 带 tool_calls → 模型请求调用工具            │
    │ 3. 本地执行工具，把结果以 role=tool 消息追加进历史     │
    │ 4. 回到第 1 步，让 LLM 看着工具结果继续思考           │
    └─────────────────────────────────────────────────┘

两个关键认知（初学者最容易忽略的点）：

  * LLM 本身是【无状态】的。所谓"多轮记忆"，全靠客户端每次把完整
    消息历史重新发一遍。历史里少一条，模型就"忘"了一条。
  * 模型返回的工具调用【不会被自动执行】。它只是输出了一段结构化的
    "请求"，真正执行的是我们本地的代码，执行完还要把结果喂回去。

Function Calling、Tool Use、ReAct……底层都是这个循环的不同包装。
"""

import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from code_tools import prepare_workspace
from memory import memory_dir, memory_index_block
from permissions import ALLOW, ASK, DENY, PermissionGate, Verdict, rejection_result
from subagent_tools import (SUBAGENT_MAX_PARALLEL, SUBAGENT_REPORT_MAX_CHARS,
                            SUBAGENT_TOOLSET)
from system_prompt import SUBAGENT_SYSTEM_PROMPT, SYSTEM_PROMPT
from tools import TOOL_SCHEMAS, ToolContext, error_result, execute_tool, is_read_only, tool_schema
from ui import colored

log = logging.getLogger("agent")  # 输出目的地由 logger.py 统一配置（写入 agent.log）

# ---------------------------------------------------------------------------
# 上下文压缩（auto-compaction）
#
# 历史越长，每轮请求越贵、越慢，最终撑爆模型窗口。做法：一轮回答结束后，
# 若估算的 prompt tokens 超过窗口 80%，就把"除首条用户消息外的中段历史"交给
# 同一个 LLM 总结成一条摘要，之后发给模型的消息列表里用摘要【替代】被压缩段。
#
# 核心原则——两套视图一个真相：数据库和前端时间线永远保留完整历史（真相），
# 压缩只发生在 _messages_for_model() 构建的"模型视图"里。物理上不删任何消息，
# 只往历史里插入一条 role="compact" 的边界标记；构建视图时遇到边界，就跳过
# 它之前的消息、换入标记里的摘要。想"撤销"或换种压缩策略，历史原封不动还在。
# ---------------------------------------------------------------------------

COMPACT_THRESHOLD = 0.8   # 估算 prompt tokens 占窗口的比例超过它 → 触发压缩。
                          # 不设 0.9+：估算本身有误差（字符反推），还得给输出留余量。
COMPACT_KEEP_TAIL = 6     # 压缩时原样保留最近几条消息：模型的"工作记忆"，
                          # 正在进行的修改细节不能靠摘要转述（转述必有损）。
COMPACT_MIN_SEGMENT = 4   # 可压缩段最少几条消息：太短说明刚压完又触发（窗口太小），
                          # 硬压会陷入"压不出空间→再压"的死循环，不如放弃。

COMPACT_SUMMARY_NOTE = "【早期对话已压缩】以下是更早历史的摘要，替代原始消息（原文仍存于数据库）："

# 压缩摘要连续失败的熔断阈值：连续这么多次失败（网络/余额/服务商故障）后
# 停止自动压缩尝试——每次失败都要等一次超时/报错，回合收尾被无谓拖慢；
# 阈值状态随下一次成功自然清零，进程重启也清零。
MAX_COMPACT_FAILURES = 3

# compact 后文件重注入的预算（参照 ZCode compact-post-reminders 的量级）：
# 最多带最近读过的 5 个文件，单文件正文截 12000 字符（约 5K token），全部
# 注入合计 48000 字符封顶；超预算的文件降级为一行"需要时重新 read_file"。
RECENT_READS_KEEP = 12       # 内存里保留最近 N 次 read_file 记录
REINJECT_MAX_FILES = 5
REINJECT_FILE_CHARS = 12_000
REINJECT_TOTAL_CHARS = 48_000

# CJK 感知 token 估算：无真实 usage 校准时的默认口径。
# 中文（含日文假名/韩文）在 GLM 等分词器里约 1 字 ≈ 0.6-1 token，拉丁/数字
# 约 4 字符 1 token。公式 ceil((CJK×2 + 其他)/3)：纯中文 ≈ 0.67 token/字，
# 纯英文 ≈ 0.25 token/字符，混合文本按占比插值。比旧的统一 0.4 系数准——
# 旧口径对中文系统性低估约一半，压缩与清理因此迟到。
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\uff01-\uffe5]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_CHAR_RE.findall(text))
    other = len(text) - cjk
    return (cjk * 2 + other + 2) // 3

# ── 分级压缩：先"清旧工具结果"（无损），仍超再摘要（有损）────────────────
# 背景：工具结果（read_file 的全文、run_bash 的输出、grep 的命中列表）往往
# 是上下文里最占地方的部分，但它们的价值随轮次迅速衰减——模型看完就用了，
# 之后很少回头再读。相比之下，"摘要"会把任务目标、改动清单一起重写，是有损
# 的。所以分级：超阈值先做便宜的清理，清理后仍超阈值才动摘要。
#
# 清理规则：保留最近 KEEP_RECENT 条工具结果原样（模型的工作记忆），更早的
# 把正文换成一行占位符。注意【不能删消息本身】——tool 消息与前面的
# assistant.tool_calls 是配对的，删了会出现"没有请求却冒出结果"的悬空消息，
# 服务端直接 400。所以只换内容，保留 role/tool_call_id 骨架。
CLEAR_TOOL_RESULTS_KEEP_RECENT = 8   # 最近的 N 条工具结果原样保留
CLEAR_TOOL_RESULTS_MIN_SAVING = 2000  # 预估至少省这么多字符才值得动手（否则白折腾）
CLEARED_TOOL_RESULT_PLACEHOLDER = "[较早的工具结果已清理以节省上下文；如需该内容请重新调用相应工具]"

SUMMARIZE_PROMPT = """\
你是对话压缩器。下面是本任务较早的对话记录（含工具调用与结果）。请把它压缩成一份 \
给"之后继续这个任务的助手"看的中文备忘，它会替代原始历史发给模型。必须包含以下小节：
1. 任务目标：用户最初要求做什么，后续追加或修改过哪些要求；
2. 已完成的改动清单：创建/修改过哪些文件、执行过哪些关键命令及其结论；
3. 关键文件路径、函数/变量名、重要事实（报错原因、验证是否通过等）；
4. 用户的全部发言：按时间顺序【逐字】列出用户说过的每一句话（简短的"继续/好的" \
可以合并注明，其余不得改写——用户的原话是后续判断意图的唯一依据）；
5. 安全与偏好红线：用户明确禁止过的做法、要求的代码风格与沟通偏好，逐字保留；
6. 未完成事项与下一步计划。
最后一节必须是「下一步」，且只能基于记录末尾（最近几条消息）的实际进展给出， \
不得凭空编造新计划。用简洁的条目式中文输出，不要复述本提示，不要寒暄。 \
细节可以有损，但"用户原话"与"安全红线"两个小节一字都不能改写。\
"""

# ---------------------------------------------------------------------------
# 工具并行执行：同一轮 tool_calls 里【连续的只读工具】并行跑、写操作串行跑。
# 为什么这样分组是安全的：正确性论证见 Agent._execute_tool_calls；
# 每个工具的 read_only 标记登记在 tools.py 的 TOOL_READ_ONLY。
# ---------------------------------------------------------------------------

PARALLEL_TOOL_WORKERS = 4  # 只读组的最大并发数：读文件/搜索以 IO 等待为主，4 个线程已足够重叠

# ── 子代理（读侧扇出，工具面与定位见 subagent_tools.py 模块注释）──────────
# 侦察任务的轮数兜底：读文件/搜索为主，通常几轮就够，15 已是宽裕上限；
# 跑满走同一套禁工具收尾轮，结论仍以模型自己的总结收场。
SUBAGENT_MAX_ROUNDS = 15

# 每个子任务挂进父 trace 的过程条目上限（sub_round/sub_tool_call/sub_tool_result
# 合计）。父 trace 落库时整体 150 条封顶（app._persist_trace），3 个并发子代理
# 放任写入会把父回合自己的过程挤出轨迹——超限追加一条 sub_truncated 省略标记。
SUB_TRACE_MAX_ENTRIES = 30

# 单条工具结果进入历史的长度上限（字符）。这是最后的安全闸：各工具内部虽有
# 各自的输出上限（MAX_READ_LINES / MAX_OUTPUT_CHARS 等），但工具众多、口径
# 不一，且 run_bash `cat 100MB文件` 这类组合仍可能漏出巨型结果。巨型结果一旦
# 落进历史，之后每轮请求都原样重复携带——除了撑爆上下文，还实测触发过供应商
# 内容风控（browser-profiles 里扩展文件的域名表被整读进历史 → 全会话 400，
# 换模型无效，因为污染在 messages 里）。截断保留头部并显式告知余量。
MAX_TOOL_RESULT_CHARS = 60_000

# ── 结果落盘轻引用（①60k 截断的升级档）──────────────────────────────────
# 超过内联阈值的工具结果：全文经 result_sink 落盘（db.write_tool_result，
# app.py 注入），历史里只内联头部预览 + full 引用，模型按需用
# read_tool_result 工具按行分段读回——"截断即丢失"变成"截断即引用"。
# 阈值显著低于 60k 才有意义：16k 字符约 5-8k token，作为"单条工具结果在
# 上下文里的常驻成本"的上限是合适的量级。落盘失败（磁盘异常）退回旧截断，
# 绝不让存储问题打断工具链路。result_sink 未注入（CLI/单测）时整体退回旧行为。
TOOL_RESULT_EXTERNALIZE_CHARS = 16_000
TOOL_RESULT_PREVIEW_CHARS = 4_000

# ---------------------------------------------------------------------------
# 防失控与收尾（参照 ZCode 的设计哲学：防失控靠「模式检测 + 注入提醒让模型自纠」，
# 不靠计数砍停；轮数上限只负责兜底，到限走「禁工具的收尾轮」，回合永远以真实
# 总结收场）。
#
# 合成消息（_synthetic: true 标记）的生命周期硬性不变式：
#   * 只由运行时构造（收尾指令、循环/预算提醒），用户输入永不带此标记；
#   * 发送给模型：保留（_clean_outgoing 剥掉全部下划线前缀键，自动剥离）；
#   * 落库：跳过（db.save_messages 不写 _synthetic 行）——重启恢复后历史里没有
#     它，DB 仍是时间线唯一真相，前端零改动；
#   * SSE：提醒类合成消息不发任何事件；收尾轮照常发 round/answer_delta/usage/done
#     （worker 的 seg_mid 依赖 round 事件，必须发）；
#   * 记忆提取：提取输入跳过 _synthetic（不是用户说的话，不该被提炼成记忆）；
#   * 压缩：无需特殊处理——合成 user 消息位于 tool 结果之后，是合法压缩切点。
# ---------------------------------------------------------------------------

REPEAT_STREAK_REMIND = 3   # 同一工具 + 相同参数连续重复达到该次数 → 注入循环提醒
MAX_TURN_REMINDERS = 3     # 每回合全部合成提醒的总预算（循环提醒与轮数预算提醒共享）

WRAP_UP_INSTRUCTION = ("本轮工具调用轮数已达上限（{max_rounds}）。不要再调用任何工具——"
                       "请基于以上已获得的信息：①总结目前已完成或已修改的内容；"
                       "②指出未完成的部分和下一步建议。"
                       "③若任务明显尚未完成，请在总结末尾用无序列表逐条列出【剩余步骤】，"
                       "让用户能据此一句话让我继续（不要写\"如需继续请告知\"这类空话）。"
                       "直接输出总结。")

REPEAT_REMIND_TEXT = ("（系统提示：你已连续 {count} 次以完全相同的参数调用工具 {name}。"
                      "不要原样重试——基于已有结果换一个做法：调整参数、换工具、"
                      "说明阻塞在哪里，或直接向用户汇报。）")

BUDGET_REMIND_TEXT = ("（系统提示：本轮已进行到第 {round_no} 轮 / 上限 {max_rounds} 轮。"
                      "请开始收敛：优先完成核心改动，规划好剩余步骤，避免再做大范围探索。）")

# 被拒调用原样重试的提醒（3b）：比 REPEAT_REMIND_TEXT 更早触发——不等连续 3 次，
# 「上一轮刚被拒、这一轮又原样调」即刻提醒。实测模型会无视拒绝结果里的 hint 再调
# 一次同一工具，白等一个权限确认超时（300s），这条提醒是拦它的第一道软闸。
DENIED_REMIND_TEXT = ("（系统提示：你刚刚被拒绝的工具调用 {name} 又原样出现了一次。"
                      "不要重复发起同一个被拒请求——换等价的安全做法，或直接向用户"
                      "说明「需要你授权 X」并结束本轮等待回复。）")

# 轮数上限：soft = 初始上限（到限先尝试续轮），到 soft 时有实质进展则每次续
# ROUND_EXTEND_STEP 轮，最多续 MAX_ROUND_EXTENSIONS 次；硬顶 = soft 的兜底，
# 到硬顶才真正进入收尾轮。见 _run 的主循环。
ROUND_EXTEND_STEP = 20
MAX_ROUND_EXTENSIONS = 2

# 续轮提醒：soft 到限但任务在推进时注入，告知模型上限已延长、要抓紧收口。
ROUND_EXTEND_REMIND_TEXT = ("（系统提示：第 {round_no} 轮仍在有效推进，轮数上限已从"
                            "原值延长至 {new_limit} 轮。请继续完成剩余步骤，但注意"
                            "抓紧收口、优先做核心改动，避免无谓的大范围探索。）")


class Agent:
    """一个带工具调用能力的对话 Agent。

    参数：
        llm:            提供 chat(messages, tools) -> dict 的客户端（llm_client.py）
        max_rounds:     单次提问内最多"问 LLM"几轮。它只负责兜底：跑满后不再硬砍，
                        而是注入合成指令进入「收尾轮」，让回合以模型自己的真实总结
                        收场（防反复空转另有重复指纹提醒，见 REPEAT_STREAK_REMIND）
        verbose:        是否在终端打印每一轮的思考/工具调用过程（学习时强烈建议开着）
        workspace:      本会话的工作区目录（文件/命令工具的边界）。不传 = 默认工作区
                        （项目 workspace/）。每个任务各自解析，
                        互不共享——这是多会话隔离的关键。
        vision_backend: fn(image_parts, question) -> str，analyze_image 工具的"看图"
                        后端，由 app.py 按当前模型配置提供；命令行版不传（无图可看）。
        context_window: 当前模型的上下文窗口（token）。非零时启用自动压缩：
                        回答结束后估算超 80% 就把中段历史总结成摘要（0 = 不压缩）。
        artifact_reader: fn(rel_path) -> dict，外置大消息的还原器（db.read_artifact），
                        由 app.py 注入；不传 = 无还原能力（遇到归档消息退回 head/tail
                        预览文字）。Agent 本身不依赖存储层——命令行版不传。
        permission_gate: PermissionGate 实例（permissions.py，最小权限闸门）。
                        不传时按内置默认规则自建一个、ask 等待上限为 0——即
                        「高危命令直接带原因拒绝」，命令行版没有网页确认卡片，
                        宁可拒绝改道也不挂死终端；Web 版由 app.py 注入带用户
                        规则加载器、可等待用户决定的正式闸门。
    """

    def __init__(self, llm, system_prompt: str = SYSTEM_PROMPT,
                 max_rounds: int = 40, verbose: bool = True, vision_supported: bool = True,
                 workspace=None, vision_backend=None, context_window: int = 0,
                 artifact_reader=None, permission_gate=None, session_id=None,
                 model_tag: tuple[str, str] | None = None,
                 allowed_tools: tuple[str, ...] | None = None,
                 result_sink=None):
        # max_rounds=40：上限只是兜底（真失控另有指纹提醒拦截），合法的长任务
        # （读代码→改→跑验证→再修）经常要几十轮，40 是给它们的余量；到限走
        # 收尾轮（_wrap_up_round）而不是"强制停止"。
        self.llm = llm
        self.system_prompt = system_prompt
        # 当前模型标识（app.py 构建时传入）。只做一件事：随 _stats 落库，让
        # message_usage 能按供应商/模型聚合出用量页（会话中途切模型也能正确归组）。
        self.model_tag = model_tag or ("", "")
        self.max_rounds = max_rounds
        self.verbose = verbose
        self.vision_supported = vision_supported  # 激活模型能否直接看图（决定是否剥离图片输入）
        self.context_window = int(context_window or 0)  # 压缩触发线的基准（providers 表解析链提供）
        self.history: list[dict] = []  # 不含 system 的完整对话历史，跨提问持续累积
        self.trace: list[dict] = []    # 最近一次提问的过程轨迹（轮次/工具调用），供前端展示
        # 上一轮被权限拒绝的调用指纹（3b）：_run 每回合重置，这里给个默认值兜住
        # 「未经 _run 直接调 _execute_tool_calls」的单测/子路径。
        self._denied_sigs: set[str] = set()
        # 进行中回合的推理累积缓冲（按轮次分段，字符串）——trace 里的 reasoning
        # 条目要等整轮流结束才写入，进行中快照（app.py 的节流落库）靠它取到
        # 「已吐出的思考文本」。round_no → str；流结束时被 _consume_stream 聚合
        # 进 trace 后清零对应键。
        self.reasoning_live: dict[int, str] = {}
        # 增量落盘的指纹账本 {mid: sha1}：save_messages 靠它识别"这条已写过、
        # 内容没变"，每轮只落新增。跨轮随实例存活；进程重启后由恢复的历史重建
        # （db.fingerprints，见 app.py 的会话恢复）。值由 db.save_messages 维护。
        self.saved: dict[str, str] = {}
        # 外置大消息还原器（db.read_artifact）。历史里的归档消息（_artifact 标记，
        # 由 save_messages 落盘时就地替换而来）只有 head/tail 摘要，构造模型视图
        # 时靠它把完整正文读回来。存储注入而非直接 import db：本文件保持存储无关，
        # 命令行版与单测不引 db 也能跑。
        self.artifact_reader = artifact_reader
        # 工具结果落盘 sink（fn(content) -> {path, bytes, chars}，db.write_tool_result
        # 由 app.py 注入）：超内联阈值的工具结果全文落盘、历史留轻引用（见
        # _externalize_tool_result）。与 artifact_reader 同构的存储注入——不传 =
        # 无落盘能力，退回旧的 60k 截断（命令行版与单测保持旧行为）。
        self.result_sink = result_sink
        # 事件/用量外发 sink（app.py 在 _run_round 开始时注入，CLI/单测为 None）：
        # event_sink 把子代理过程事件推上会话事件总线（嵌套 trace，见
        # _run_one_subagent_inner）；usage_sink 把子代理/压缩等隐藏 LLM 消耗记进
        # message_usage（用量归账，见 _record_usage）。挂在实例而非构造参数：
        # event_sink 要耦合 worker 的进行中快照节流（_maybe_snapshot），归 _run_round 管。
        self.usage_sink = None   # fn(kind: str, stats: dict) -> None
        self.event_sink = None   # fn(event: dict) -> None
        # 每字符 token 校准系数（真实 prompt_tokens ÷ 当次请求总字符数）。跨轮缓存：
        # 压缩判断发生在回答结束后，那时没有新 usage，只能靠上一轮校准的系数估算。
        # 压缩后置回 None——摘要的 token 密度与原始日志完全不同，旧系数必然失真，
        # 等下一轮真实 usage 重新校准（见 context_stats / _maybe_compact）。
        self._token_ratio: float | None = None
        # 工具白名单（子代理专用）：None = 全量工具面（主代理）；给了名字集合
        # 则 schema 与执行两侧都只暴露这个子集（见 _tool_schemas / _run_tool）。
        # 子代理传 SUBAGENT_TOOLSET（只读侦察面）——schema 是"模型能看见什么"
        # 的唯一来源，看不见的工具模型调不到；执行侧再把关一道是防幻觉调用。
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        # 记忆索引的回合快照（_run 开始时刷新）。system 是每轮请求的前缀头，
        # 供应商的前缀缓存要求它逐字节稳定：索引若每轮从磁盘现读，模型在回合
        # 中途写一条记忆（MEMORY_CONTRACT 鼓励这么做）就会改掉 system，后面
        # 每一轮的整段历史缓存全部失效、按全价重算。快照保住前缀；跨回合的
        # 新鲜度不受影响——下一回合开始时重新快照，刚写的记忆那时自然可见。
        self._memory_snapshot: str | None = None
        # 当前时间块（_system_content 首次调用时生成并缓存，见其 docstring）。
        # 会话开始时刻的一次性快照：逐轮现取会改坏 system 前缀的 KV 缓存。
        self._time_block: str | None = None
        # 工具执行钩子（借鉴 dsh 的 pre/execute/post 三段事件，简化为两段）：
        # pre 返回拒绝原因字符串 = 否决本次调用（不执行、结果为拦截信封），
        # 返回 None = 放行；post 接 (name, arguments, result)，返回新结果串 =
        # 改写，返回 None = 保持原样（链式折叠）。改写 JSON 信封时须保持其
        # 合法——前端/trace/recent_reads 等消费方都按 JSON 解析。钩子抛异常
        # 一律忽略——钩子是扩展面（审计/沙箱策略/子代理权限继承），绝不能
        # 拖垮主循环。权限闸门【不】在这里：ask 的等待语义必须活在回合线程
        # （见 _execute_tool_calls 阶段〇），钩子只做无需等待的同步判定。
        self.pre_tool_hooks: list = []
        self.post_tool_hooks: list = [self._record_recent_read]
        # 压缩摘要连续失败计数（熔断，见 MAX_COMPACT_FAILURES）：成功清零。
        self._compact_fail_streak = 0
        # 最近一次压缩放弃的原因（_maybe_compact 各早退分支写入；/compact
        # 手动触发时反馈给用户，自动路径只进日志）。
        self._compact_skip_reason: str | None = None
        # 最近读取的文件（read_file 的实际返回片段，最旧在前，容量见
        # RECENT_READS_KEEP）：压缩后据此重注入"读过但已被摘要吸收"的文件，
        # 模型不必盲目重读就能继续任务（参照 ZCode compact-post-reminders）。
        self.recent_reads: list[tuple[str, str]] = []
        # 工具执行上下文：工作区 + 看图后端随 Agent 实例走；images 每轮提问时更新。
        # 状态挂在实例上而不是模块级全局，两个会话并发执行工具才不会串数据。
        self.ctx = ToolContext(workspace=prepare_workspace(workspace), vision_backend=vision_backend)
        self.ctx.session_id = session_id  # 文档工具据此确定文档归属（会话隔离）
        # 浏览器管理器（browser_tools）：按会话惰性创建，第一次 browser_* 工具
        # 调用才拉起 Chromium；回合/会话收尾由 app.py 调 close_session 销毁。
        # session_id 为空（CLI/单测）时也创建一个独立实例，工具统一可用。
        if session_id:
            from browser_tools import manager_for
            self.ctx.browser = manager_for(session_id)
        self.cancel_event: threading.Event | None = None  # 本轮生成的停止开关（stop() 置位）
        # 权限闸门（permissions.py）：挂实例而非模块级——规则里的工作区边界、
        # 会话内记住的 ask 决定都按会话隔离，两个会话并发各判各的。
        self.permissions = permission_gate or PermissionGate(self.ctx.workspace, ask_timeout=0.0)
        # 子代理运行器自装配（spawn_subagent 工具经 ctx 调到这里）：主代理、
        # CLI、单测构造的实例都天然带能力，app.py 无需额外接线。子代理再构造
        # 子代理会被 allowed_tools 名单在 schema 与执行两处挡住（递归上限 1 层）。
        self.ctx.subagent_runner = self._spawn_subagent

    @staticmethod
    def _clean_outgoing(m: dict) -> dict:
        """发给模型前剥离内部字段（_stats 等下划线前缀），部分服务商会拒绝未知字段。"""
        return {k: v for k, v in m.items() if not k.startswith("_")}

    def _tool_schemas(self) -> list[dict]:
        """本实例可用的工具清单。allowed_tools 为 None（主代理）时返回全量
        TOOL_SCHEMAS 原对象；子代理（白名单非 None）只返回白名单内的 schema。

        schema 是"模型能看见什么"的唯一来源——看不见的工具模型调不到，这比
        执行层拦截更根本；_run_tool 的名单把关只是防幻觉调用的第二道闸。
        context_stats 也走这里：子代理的容量估算按它实际携带的工具面算。"""
        if self.allowed_tools is None:
            return TOOL_SCHEMAS
        return [s for s in TOOL_SCHEMAS
                if s.get("function", {}).get("name") in self.allowed_tools]

    def _system_content(self) -> str:
        """实际发给模型的 system 内容 = 系统提示词 + 持久记忆索引段。

        提示词正文与记忆契约都在 SYSTEM_PROMPT（system_prompt.py）：契约以
        import 方式拼在其末尾而非复制副本，memory.py 改契约常量两边自动同步。
        这里只补【动态】的索引段（memory_index_block）——它用回合开始时的
        快照（_run 里刷新，见 _memory_snapshot 的缓存论证），不在回合中途
        现读磁盘；构造后没跑过回合（直接单测 _system_content）时退回现读，
        行为与旧版一致。契约由此在 system 里恰好出现一次（既不在块里
        重复，也不会漏掉）。

        关键不变式：记忆只进 system 消息，绝不进消息历史——上下文压缩只重写
        消息历史的模型视图（_visible_history）、从不修改 system，因此 compact
        之后记忆原样保留，也不会被重复注入。

        当前时间块：早期的 current_time 工具（已移除）让模型"问一次时间花一轮
        调用"，而这里按会话开始时刻一次性注入、逐字节缓存——模型随时知道
        "今天是几号"，长会话里流逝的分钟数不值得用"每轮改坏 system 前缀缓存"
        去换精确。排在记忆索引之后：最稳定的部分在前（前缀缓存从头部命中）。
        CLI（cli.py）与 Web（app.py）都不传 system_prompt，默认值即
        SYSTEM_PROMPT，注入自动生效；轮末【自动提取】目前只挂 Web worker
        （app.py _run_round 收尾处），CLI 不触发——后续要挂时调
        memory.run_extraction_async 即可，是同一个钩子。
        """
        block = (self._memory_snapshot if self._memory_snapshot is not None
                 else memory_index_block(memory_dir(self.ctx.workspace)))
        if self._time_block is None:
            now = datetime.datetime.now()
            self._time_block = (
                f"\n\n【当前时间】{now:%Y-%m-%d %H:%M} 周{'一二三四五六日'[now.weekday()]}"
                "（会话开始时间；此后经过的时长请按对话推进自行估算）")
        return self.system_prompt + block + self._time_block

    def _visible_history(self) -> list[dict]:
        """模型视图的"该看哪些消息"——压缩的唯一生效点（纯函数，测试覆盖）。

        规则：history 里最后一条 role="compact" 的边界标记定义压缩段；
        视图 = [最初一条用户消息（原始需求，逐字保留）] + [边界里的摘要（以
        user 身份注入）] + [边界之后的所有消息]。边界之前的历史（含旧边界
        和已被旧摘要吸收的段落）对模型不可见，但物理上一条都没删。

        两个不能破坏的点（违反哪个，任务都会"失忆"或直接报错）：
        1. 最初一条用户消息逐字保留——它是整个任务的锚点，总结必有损，原件不能丢；
        2. 只认【最后一条】边界——连续压缩时，新摘要是把"旧摘要+其后的消息"
           一起再总结的产物，旧摘要已被吸收，旧边界自然失效。
        """
        last_boundary, first_user = -1, -1
        for i, m in enumerate(self.history):
            if m.get("role") == "compact":
                last_boundary = i  # 取最后一条：循环结束时留的就是最靠后的边界
            elif first_user < 0 and m.get("role") == "user":
                first_user = i
        if last_boundary < 0 or first_user < 0 or last_boundary <= first_user:
            # 从未压缩过（或历史异常，比如边界跑到首条消息前面）→ 原样全量返回
            return self.history
        summary = self.history[last_boundary].get("content") or ""
        view = [self.history[first_user],
                {"role": "user", "content": f"{COMPACT_SUMMARY_NOTE}\n{summary}"}]
        view.extend(self.history[last_boundary + 1:])
        return view

    def _expand_artifact(self, m: dict) -> dict:
        """还原一条外置归档消息为完整消息（模型视图专用）。

        存储层把超过阈值的超大消息正文挪进了 artifacts 文件（行内只留
        head/tail 摘要，见 db.save_messages）。外置只影响存储与前端展示，
        【不改变模型看到的内容】——构造请求前必须把完整正文读回来，否则
        模型的上下文里会凭空缺一大块（比如某轮 run_bash 的完整输出）。

        容错：归档文件被误删/损坏时退回 head+tail 拼接的文字并注明截断——
        一次读盘失败绝不能打断整轮对话，模型看到"输出被截断"仍能继续工作。
        """
        fallback = {"role": m.get("role", "assistant"),
                    "content": (m.get("head") or "") + "\n…[内容过大已归档，完整原文读取失败，以上为开头部分]\n"
                    + (m.get("tail") or "")}
        if self.artifact_reader is None:
            return fallback
        try:
            full = self.artifact_reader(m.get("path") or "")
            return full if isinstance(full, dict) and full.get("role") else fallback
        except Exception as e:
            log.warning("归档消息还原失败（path=%s）：%s", m.get("path"), e)
            return fallback

    def _messages_for_model(self) -> list[dict]:
        """发给 LLM 的消息列表。四件事：
        1. 换入压缩视图：被压缩的段落用摘要替代（见 _visible_history，DB 原文不动）；
        1.5 视图内的外置归档消息（_artifact 摘要行）先还原成完整消息——
            外置只是存储优化，模型必须看到当初的完整内容；窗口外的归档消息
            连视图都不进，自然不还原（白省一次读盘）；
        2. 剥离内部字段（_stats 等下划线前缀），部分服务商会拒绝未知字段；
        3. 主模型不支持视觉时，把用户消息里的图片部分替换成文字提示——
           否则不支持视觉的服务商会对图片输入直接报 400。"""
        sanitized = []
        for m in self._visible_history():
            if m.get("_artifact"):
                m = self._expand_artifact(m)  # 还原后同样走下面的剥离/视觉处理
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, list) and not self.vision_supported:
                texts, has_image = [], False
                for part in content:
                    if part.get("type") == "image_url":
                        has_image = True
                        continue
                    if part.get("type") == "text" and part.get("text"):
                        texts.append(part["text"])
                note = "\n".join(texts)
                if has_image:
                    note += "\n[用户上传了一张图片；你看不到它的内容，请调用 analyze_image 工具来识别]"
                sanitized.append(self._clean_outgoing({"role": "user", "content": note}))
            else:
                sanitized.append(self._clean_outgoing(m))
        return sanitized

    def context_stats(self, prompt_tokens: int | None = None) -> dict:
        """估算当前上下文的构成（各部分的 token 份额）。

        两级口径：
        1. 有服务商返回的真实 prompt_tokens 时，先用 它/总字符数 校准出每字符
           token 系数，再按各部分字符数分摊——这是最准的（随模型/语言自适应）；
        2. 无校准值时（会话第一轮、压缩后系数作废期）用 CJK 感知估算
           （estimate_tokens）：中文约 0.67 token/字、拉丁约 0.25 token/字符。
           旧版此处统一按 0.4 token/字符粗估，对中文系统性低估约一半——
           压缩与清理因此迟到，用户先看到的是账单暴涨。

        两处与压缩相关的口径：
        1. 字数统计基于【模型视图】而非原始 history——压缩后模型看到的已是摘要，
           按原始历史估算会永远超阈值、反复触发无意义的压缩；
        2. 校准系数缓存在 self._token_ratio（回答结束后的压缩判断靠它），压缩后作废。
        """
        view = self._messages_for_model()
        # system 口径必须与实际请求一致：含记忆段（契约 + 索引），否则记忆
        # 越攒越多时压缩触发线会被系统性低估
        sys_text = self._system_content()
        tool_text = json.dumps(self._tool_schemas(), ensure_ascii=False)
        chars = {"user": 0, "assistant": 0, "tool": 0}
        toks = {"user": 0, "assistant": 0, "tool": 0}
        for m in view:
            text = str(m.get("content") or "")
            calls = json.dumps(m.get("tool_calls") or "", ensure_ascii=False)
            if m["role"] in chars:
                chars[m["role"]] += len(text) + len(calls)
                toks[m["role"]] += estimate_tokens(text) + estimate_tokens(calls)
        total_chars = max(1, len(sys_text) + len(tool_text) + sum(chars.values()))
        if prompt_tokens:
            ratio = prompt_tokens / total_chars
            self._token_ratio = ratio  # 缓存：本轮之后的压缩判断用它估算
        else:
            ratio = self._token_ratio  # 可能为 None（首轮/压缩后作废期）
        if ratio:
            # 校准态：按字符分摊（随模型/语言自适应，最准）
            out = {k: round(v * ratio) for k, v in chars.items()}
            out["system"] = round(len(sys_text) * ratio)
            out["tools"] = round(len(tool_text) * ratio)
        else:
            # 未校准：CJK 感知逐段估算（对中文远比统一字符系数准）
            out = dict(toks)
            out["system"] = estimate_tokens(sys_text)
            out["tools"] = estimate_tokens(tool_text)
        return out

    # ------------------------------------------------------------------

    def run(self, user_input: str, user_message: dict | None = None):
        """生成器版核心循环：边跑边产出事件，供 Web 流式接口实时推给前端。

        user_message：完整的用户消息（OpenAI 格式，content 可以是带图片/文件的
        数组）。不传则用 user_input 包装成纯文本消息——CLI 走这条路。

        事件序列（kind, payload)：
          ("round",           {"round": n, "wrap_up"?: True})    开始第 n 轮；wrap_up=True
                                                              标记收尾轮（跑满 max_rounds
                                                              后的禁工具总结轮）
          ("reasoning_delta", {"delta": "..."})              思考模型的推理片段（仅实时展示，不进历史；
                                                             worker 转发时不带 mid——推理不是回答）
          ("answer_delta",    {"delta": "..."})              LLM 正在输出的文字片段
          ("tool_call",       {"name", "arguments"})         模型请求调用工具
          ("permission_request", {"id", "tool", "input",     权限闸门命中 ask：回合
                                  "reason"})                 在此暂停等用户决定
                                                             （恢复后继续，见下）
          ("tool_result",     {"name", "result"})            工具执行结果
          ("usage",           {...})                         token 用量 / 上下文构成 / 缓存命中
          ("done",            {"answer": "...", "stopped"?,  最终回答，循环结束；
                               "stopped_reason"?})            用户中途停止时带 stopped=True；
                                                              收尾轮正常结束带
                                                              stopped_reason="max_rounds"
          ("compacted",       {"summary", "prompt_tokens",   done 之后可能跟上：回答结束、
                               "context"})                   上下文超阈值已自动压缩（不产生
                                                              回答流，前端渲染分隔卡片）

        循环提醒 / 轮数预算提醒（_maybe_remind 注入的合成 user 消息）不产生任何
        事件——它们只进历史与下一轮的模型请求，前端零感知。        """
        # 停止开关：每次提问配一个新 Event，stop() 置位后循环尽快带部分结果收尾；
        # 结束（或生成器被关闭）时也置位，让 llm_client 里的看护线程退出。
        self.cancel_event = threading.Event()
        stopped = False
        try:
            for kind, payload in self._run(user_input, user_message):
                if kind == "done":
                    stopped = bool(payload.get("stopped"))  # 记下结局，循环外决定是否压缩
                yield kind, payload
            # 压缩时机：一轮回答【完全结束】之后（done 已产出、不在流式过程中）——
            # 用户已拿到回答，此刻多花一次 LLM 调用不影响体验；也绝不能在循环内做，
            # 否则"正在打字的回答"会被一次几十秒的静默总结卡住。
            # 用户主动停止时不压缩：他刚表达"想停下"，别再让他等一次隐性调用，
            # 超阈值状态留着，下一轮回答结束后自然重试。
            if not stopped and not self.cancel_event.is_set():
                compacted = self._maybe_compact()
                if compacted:
                    yield "compacted", compacted
        finally:
            self.cancel_event.set()

    def stop(self) -> None:
        """请求停止当前这轮生成（Web「停止」按钮 → /api/chat/stop 调到这里）。"""
        if self.cancel_event is not None:
            self.cancel_event.set()

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        """用户对一次权限确认的决定（Web 端点 → 这里 → 闸门唤醒等待中的回合）。

        本方法跑在 HTTP 线程、只在闸门上 set 一个 Event——回合线程正阻塞在
        wait_all 的分片等待上，会被及时唤醒；此处不碰历史、不持会话锁。
        未知 id / 已处理过 / 非法取值返回 False（超时后的迟到点击、补发重放
        出的旧卡片都安全落在这里，由前端提示"确认已失效"）。
        """
        return self.permissions.resolve(request_id, decision)

    def _run(self, user_input: str, user_message: dict | None = None):
        """run() 的实际循环体（run 只负责停止开关的生命周期）。"""
        self.trace = []  # 每次提问重新记录过程轨迹
        self.reasoning_live = {}  # 进行中快照的推理缓冲同步重置（见 __init__ 注释）
        self.history.append(user_message or {"role": "user", "content": user_input})
        # 回合开始的两个"前缀稳定"动作，各自只在本回合做这一次——之后本回合
        # 的全部请求前缀逐字节不变，这是供应商自动前缀缓存能命中的前提：
        # ① 记忆索引快照：回合内模型写记忆 / 提取线程落盘都不再改 system
        #    （否则缓存全灭，见 _memory_snapshot 的论证）；
        # ② 清理上一回合的旧工具结果（分级压缩第一档提前到回合开始执行）。
        #    原本它只在估算超窗口 80% 时才触发——大窗口（如 262k）下正常对话
        #    很少到 80%，清理形同虚设，而工具输出正是每轮请求里最重的重复携带。
        #    代价是一次性的前缀缓存失效（旧消息内容变了），换来的是本回合每轮
        #    都少带几万字符——长任务下稳赚。回合内不再重复清理：边界每轮前移
        #    会把"上一轮还是原文"的消息改成占位符，前缀逐轮失效，得不偿失。
        self._memory_snapshot = memory_index_block(memory_dir(self.ctx.workspace))
        self._clear_old_tool_results()
        # 提取本轮附带的图片（OpenAI content 数组里的 image_url 部分），挂进工具上下文。
        # 主模型看不见像素；analyze_image 工具借"视觉模型"看图时用的就是这份数据。
        # 防御：调用方传来的 user_message 若已是 _artifact 摘要桩（如旧版本在回合
        # 开始落库时被就地替换），先还原成完整消息再提取——桩的 content 是 JSON
        # 字符串，直接提取会把图片静默丢成空列表（"收不到图片"事故，2026-10-10）。
        um = user_message or {}
        if um.get("_artifact"):
            um = self._expand_artifact(um)
        content = um.get("content")
        if isinstance(content, list):
            self.ctx.images = [p for p in content if p.get("type") == "image_url"]
        else:
            self.ctx.images = []
        # 回合内提醒状态（防失控，见 _maybe_remind）：重复指纹 streak、提醒预算、
        # 预算提醒轮数（上限前 10 轮、前 4 轮各提醒一次）。每回合重置。
        self._streak_sig = None
        self._streak_count = 0
        self._reminders_used = 0
        self._denied_sigs: set[str] = set()  # 上一轮被权限拒绝的调用指纹（3b）
        self._budget_remind_rounds = {self.max_rounds - 10, self.max_rounds - 4}
        start = time.time()
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                       # 缓存命中/未命中随 _stats 落库（message_usage.cached_tokens），
                       # 用量页「缓存命中」列的数据来源
                       "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
        # 跨轮回调状态（主循环与收尾轮共用一套）：缓存命中率、上下文估算、计时起点。
        # 由 _consume_stream / 两个收尾方法就地更新。
        # context_tokens：本回合【最后一次】请求的真实 prompt_tokens——那才是模型
        # 当前看到的上下文大小。usage_total.prompt_tokens 是多轮请求的累加值（每轮
        # 都重发全部历史，会被重复计数），拿它当"上下文容量"会系统性偏大。
        metrics = {"start": start, "cache_hit_rate": None,
                   "context_tokens": None,
                   "context": self.context_stats()}  # 还没发过请求时给个纯估算

        # 轮数上限的软/硬双限（1a）：soft_limit 到限时若上一轮有实质进展则续一段，
        # 续满 extensions_left 次后不再续；hard_limit 是绝对硬顶，到顶必收尾。
        # soft_limit 会随续轮增长，故用 while 而非 range；self.max_rounds 保持
        # 构造时的原值不动（跨回合复用同一 Agent 时不受上一回合续轮影响）。
        soft_limit = self.max_rounds
        hard_limit = self.max_rounds + ROUND_EXTEND_STEP * MAX_ROUND_EXTENSIONS
        extensions_left = MAX_ROUND_EXTENSIONS
        round_no = 0
        while round_no < hard_limit:
            round_no += 1
            self._round_had_progress = False  # 本轮是否发生实质进展（见下）
            if self.cancel_event.is_set():
                # 工具结果刚入完历史就被叫停：历史以上一条 tool 消息结尾，依然合法
                break
            self._log(f"── 第 {round_no} 轮：请求 LLM ──", "gray")
            self.trace.append({"type": "round", "round": round_no})
            yield "round", {"round": round_no}

            # 每轮都重发【系统提示 + 完整历史】—— 这就是 LLM 的全部"记忆"。
            # 主模型不支持视觉时，先把历史里的图片剥离成文字提示（图片数据留在
            # self.ctx.images，由 analyze_image 工具借视觉模型识别）。
            # system 末尾追加持久记忆段（契约 + 索引），不变式见 _system_content：
            # 记忆只进 system，绝不进消息历史（compact 后原样保留、不重复注入）。
            messages = [{"role": "system", "content": self._system_content()},
                        *self._messages_for_model()]
            # 完整 payload 进日志（DEBUG 级）：排错时能看到模型到底"看到"了什么
            log.debug("第 %d 轮请求 payload:\n%s", round_no,
                      json.dumps(messages, ensure_ascii=False, indent=2))

            # 流式拿模型回复：文字片段实时往外 yield，最后拿到完整 message。
            # 消费逻辑提取成 _consume_stream——收尾轮复用同一份，防止两处漂移。
            # 工具清单按实例白名单过滤（主代理=全量；子代理=只读侦察面）。
            assistant_msg = yield from self._consume_stream(messages, self._tool_schemas(),
                                                            usage_total, metrics,
                                                            round_no)
            log.debug("LLM 原始返回: %s", json.dumps(assistant_msg, ensure_ascii=False))

            if self.cancel_event.is_set():
                # 用户点了停止：已生成的半截文字直接作为回答收尾。
                # 不能把带 tool_calls 的"悬空"assistant 消息留在历史里
                # （下一轮请求会 400），所以这里只追加纯文本回答。
                yield from self._tail_stopped(assistant_msg, usage_total, metrics)
                return

            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                # 情况 a：模型直接给出回答，循环结束。
                yield from self._tail_answer(assistant_msg, usage_total, metrics)
                return

            # 情况 b：模型请求调用工具
            # 关键：这条"要求调用工具"的 assistant 消息必须原样进历史。
            # 否则下一轮历史里就出现了"没有提问却冒出 tool 结果"的悬空消息，
            # 大多数服务端会直接报 400。
            # 顺带把它的正文记为过程说明（process_text）进 trace：这类文字是
            # "我接下来要做什么"，不是最终答案——实时视图把它降级进执行过程
            # 折叠面板，回放也必须落在同一处（否则刷新后它又变回一张正文卡，
            # 与实时观感割裂）。落 trace 而非只在内存：回放靠 trace 重建过程。
            _ptext = (assistant_msg.get("content") or "").strip()
            if _ptext:
                self.trace.append({"type": "process_text", "round": round_no, "text": _ptext})
            self.history.append(assistant_msg)
            for call in tool_calls:
                self._log(f"🤖 LLM 请求调用: {call['function']['name']}({call['function']['arguments']})", "cyan")
                arguments = call["function"].get("arguments") or "{}"
                self.trace.append({"type": "tool_call", "name": call["function"]["name"], "arguments": arguments})
                yield "tool_call", {"name": call["function"]["name"], "arguments": arguments}

            # 执行工具：连续只读工具并行、写操作串行，结果按请求顺序回填
            # （分组调度规则与正确性论证见 _execute_tool_calls）
            # denied_before：本轮执行【之前】就已被拒的调用指纹——只有"上一轮
            # 被拒、这一轮又原样出现"才算重试；本轮刚被拒的那次不算（否则会在
            # 被拒的同一轮就误报一次重试提醒）。
            denied_before = set(self._denied_sigs)
            yield from self._execute_tool_calls(tool_calls)

            # 防失控提醒：不是砍停——检测到死循环苗头 / 轮数接近上限时注入合成
            # user 提醒，让模型下一轮自己纠偏（触发规则与预算见 _maybe_remind；
            # 提醒静默进历史，不发任何事件）。
            self._maybe_remind(tool_calls, round_no, denied_before)

            # 软限续轮（1a）：跑到 soft_limit 时——本轮有实质进展（成功执行了非
            # 只读工具）且还有续轮额度，就续 ROUND_EXTEND_STEP 轮并提醒模型收敛，
            # 把"能做完的长任务"从硬砍变成续命；否则【立即 break 交收尾轮】，
            # 绝不能继续跑到 hard_limit（那是"无进展也硬撑"的错误行为）。
            # 续轮同时把新的预算提醒轮次并入 _budget_remind_rounds，让收敛提醒
            # 覆盖到延长后的上限附近。hard_limit 只作绝对兜底（理论上 soft 到不了
            # 它，因为每次续轮都在 soft 处 break-or-grow）。
            if round_no >= soft_limit:
                if extensions_left > 0 and self._round_had_progress:
                    extensions_left -= 1
                    soft_limit += ROUND_EXTEND_STEP
                    self._budget_remind_rounds.update({soft_limit - 10, soft_limit - 4})
                    self._inject_reminder(ROUND_EXTEND_REMIND_TEXT.format(
                        round_no=round_no, new_limit=soft_limit),
                        "round_extend", {"round": round_no, "new_limit": soft_limit},
                        structural=True)
                    log.info("第 %d 轮有实质进展，轮数上限续至 %d（剩余续轮 %d 次）",
                             round_no, soft_limit, extensions_left)
                else:
                    break  # 到软限且无可续 → 交收尾轮（行为与旧版完全一致）

        # 走到循环外只有两种情况：被用户停止，或跑满硬顶（含续轮后的 soft_limit）
        if self.cancel_event.is_set():
            log.info("生成被用户停止（未在流式阶段截住）")
            yield "done", {"answer": "（已手动停止）",
                           "elapsed_s": round(time.time() - metrics["start"], 1),
                           "usage": usage_total, "cache_hit_rate": metrics["cache_hit_rate"],
                           "context": metrics["context"],
                           "context_tokens": metrics["context_tokens"], "stopped": True}
            return
        # 跑满上限：轮数上限的新语义是「触发收尾」而非「强制杀死」——注入合成
        # 指令，以 tools=None 请求一轮真实总结，回合以模型自己的总结 + done 收场
        # （收尾轮与普通回答同一套完成后压缩判断，见 _wrap_up_round）。
        # 传入 soft_limit：续过轮时它就是本次实际的轮数上限，收尾文案/轮号据此对齐。
        yield from self._wrap_up_round(usage_total, metrics, effective_max=soft_limit)

    # ------------------------------------------------------------------
    # 收尾轮与流的统一消费（主循环 / 收尾轮共用，防两份逻辑漂移）
    # ------------------------------------------------------------------

    def _consume_stream(self, messages: list[dict], tools: list | None,
                        usage_total: dict, metrics: dict, round_no: int = 0):
        """消费一次 LLM 流式回复。

        产出与 run() 同名的事件：answer_delta / reasoning_delta 实时透传；usage
        累计进调用方的 usage_total 并就地更新 metrics（cache_hit_rate / context，
        每次真实 prompt_tokens 都会重新校准估算系数）。生成器最终 return 完整的
        assistant 消息——调用方用
            assistant_msg = yield from self._consume_stream(...)
        事件转发与返回值一步到位。

        推理文本（reasoning_delta）累积成本轮的一条 trace 条目落库，供切换会话/
        刷新后的历史回放展示——但【绝不进 self.history】：部分服务商拒收回传的
        推理内容，回填给模型会 400。round_no 用于把该条目归到对应轮次下方。
        """
        assistant_msg = None
        reasoning_parts: list[str] = []
        for kind, payload in self.llm.chat_stream(messages=messages, tools=tools,
                                                  cancel=self.cancel_event):
            if kind == "delta":
                yield "answer_delta", {"delta": payload}
            elif kind == "reasoning_delta":
                reasoning_parts.append(payload)
                # 同步累积进 live 缓冲：进行中快照（app.py 节流落库）在流尚未
                # 结束时也能取到已吐出的思考文本，切会话回放才不缺当前轮的推理
                self.reasoning_live[round_no] = self.reasoning_live.get(round_no, "") + payload
                yield "reasoning_delta", {"delta": payload}
            elif kind == "usage":  # 本轮 token 用量 → 累计后实时推给前端
                # 缓存命中/未命中一并累计（工具循环的多次请求求和），否则落库
                # 的 _stats 里永远没有这两个键 → 用量页「缓存命中」列恒为空
                for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                    usage_total[key] = usage_total.get(key, 0) + (payload.get(key) or 0)
                hit = payload.get("prompt_cache_hit_tokens")
                miss = payload.get("prompt_cache_miss_tokens")
                if hit is not None and (hit + (miss or 0)) > 0:
                    metrics["cache_hit_rate"] = round(hit / (hit + miss) * 100, 1)
                last_prompt = payload.get("prompt_tokens") or 0
                metrics["context_tokens"] = max(metrics["context_tokens"] or 0, last_prompt)
                metrics["context"] = self.context_stats(prompt_tokens=last_prompt)
                yield "usage", {**usage_total, "elapsed_s": round(time.time() - metrics["start"], 1),
                                "cache_hit_rate": metrics["cache_hit_rate"],
                                "context": metrics["context"],
                                "context_tokens": metrics["context_tokens"]}
            else:
                assistant_msg = payload
        # 本轮推理文本收尾后整段入 trace（放流结束而非每个 delta 追加：一条条目
        # 一个轮次，回放时渲染成一个思考块）。空串不入，避免无思考模型多出空块。
        text = "".join(reasoning_parts)
        if text:
            self.trace.append({"type": "reasoning", "round": round_no, "text": text})
        # 本轮推理已固化进 trace：从 live 缓冲摘除，避免快照双份携带
        self.reasoning_live.pop(round_no, None)
        return assistant_msg

    def _tail_stopped(self, assistant_msg, usage_total: dict, metrics: dict):
        """「用户中途停止」收尾：已生成的半截文字直接作为回答。

        不能把带 tool_calls 的"悬空"assistant 消息留在历史里（下一轮请求会 400），
        所以这里只追加纯文本回答。主循环与收尾轮共用。"""
        partial = (assistant_msg or {}).get("content") or ""
        answer = (partial + "\n\n（已手动停止）").strip()
        elapsed = round(time.time() - metrics["start"], 1)
        self.history.append({"role": "assistant", "content": answer,
                             "_stats": {"elapsed_s": elapsed, "usage": dict(usage_total),
                                        "cache_hit_rate": metrics["cache_hit_rate"],
                                        "context_tokens": metrics["context_tokens"],
                                        "provider_id": self.model_tag[0], "model": self.model_tag[1],
                                        "stopped": True}})
        log.info("耗时 %.1fs · 用户中途停止", elapsed)
        yield "done", {"answer": answer, "elapsed_s": elapsed, "usage": usage_total,
                       "cache_hit_rate": metrics["cache_hit_rate"],
                       "context": metrics["context"],
                       "context_tokens": metrics["context_tokens"], "stopped": True}

    def _tail_answer(self, assistant_msg, usage_total: dict, metrics: dict,
                     stopped_reason: str | None = None):
        """「模型直接给出回答」收尾：统计随消息一起存进历史（_stats 前缀 = 内部
        字段，发送给模型前会被剥离，见 _messages_for_model），回放时可见。

        stopped_reason：非 None 时附加到 done payload（收尾轮用它标记
        "max_rounds"），其余字段与正常 done 完全一致。"""
        answer = assistant_msg.get("content") or ""
        # 空回答兜底：模型这一轮只产出了推理、没有正文（或推理被服务商混进
        # content 后由 llm_client 剥离）时，不能把空串当回答——前端 done 分支
        # 会用 evt.answer 整体覆盖气泡，空串会留下一句也没说的空白气泡。
        # 这里换成一句明确说明，落库与展示都自洽。
        if not answer:
            answer = "（本轮没有产出文字回答；推理过程见执行过程）"
            log.warning("本轮 LLM 未返回正文内容，已用占位说明收尾")
        elapsed = round(time.time() - metrics["start"], 1)
        self.history.append({"role": "assistant", "content": answer,
                             "_stats": {"elapsed_s": elapsed, "usage": dict(usage_total),
                                        "cache_hit_rate": metrics["cache_hit_rate"],
                                        "context_tokens": metrics["context_tokens"],
                                        "provider_id": self.model_tag[0], "model": self.model_tag[1]}})
        log.info("耗时 %.1fs · tokens 输入 %d / 输出 %d",
                 elapsed, usage_total["prompt_tokens"], usage_total["completion_tokens"])
        payload = {"answer": answer, "elapsed_s": elapsed, "usage": usage_total,
                   "cache_hit_rate": metrics["cache_hit_rate"], "context": metrics["context"],
                   "context_tokens": metrics["context_tokens"]}
        if stopped_reason:
            payload["stopped_reason"] = stopped_reason
        yield "done", payload

    def _wrap_up_round(self, usage_total: dict, metrics: dict,
                       effective_max: int | None = None):
        """收尾轮：跑满轮数上限后注入合成 user 指令，以 tools=None 请求一轮
        真实总结，让回合以模型自己的总结收场（替换旧的"强制停止"兜底文案）。

        effective_max：本次实际跑到的轮数上限（1a 续轮后可能 > self.max_rounds）。
        收尾文案的"上限"与收尾轮号都以它为准，避免续过轮却对外宣称"达上限 40"。

        1. 合成消息（_synthetic 标记）的生命周期不变式见模块头注释——这里只
           负责构造与追加，剥离（发给模型）/跳过（落库/提取）都在下游自动生效；
        2. 收尾轮照常发 round / answer_delta / usage / done 事件（worker 的
           seg_mid 依赖 round 事件换回答气泡，必须发）；
        3. 收尾途中被停止 → 与主循环同一套「用户中途停止」收尾；chat_stream 抛
           RuntimeError → 原样上抛，worker 的错误路径（error + turn_end）收尾，
           合成消息未落库，历史状态依然合法；
        4. 正常结束 → done 带 stopped_reason="max_rounds"；run() 对 done 的
           压缩判断一视同仁（未被停止就走 _maybe_compact），不另起路径。
        """
        limit = effective_max if effective_max is not None else self.max_rounds
        round_no = limit + 1
        self.history.append({"role": "user", "_synthetic": True,
                             "content": WRAP_UP_INSTRUCTION.format(max_rounds=limit)})
        log.warning("达到最大轮数 %d，进入收尾轮（禁工具总结）", limit)
        self._log(f"── 第 {round_no} 轮（收尾）：请求总结 ──", "gray")
        self.trace.append({"type": "round", "round": round_no, "wrap_up": True})
        yield "round", {"round": round_no, "wrap_up": True}
        messages = [{"role": "system", "content": self._system_content()},
                    *self._messages_for_model()]
        log.debug("收尾轮请求 payload:\n%s", json.dumps(messages, ensure_ascii=False, indent=2))
        assistant_msg = yield from self._consume_stream(messages, None, usage_total, metrics,
                                                        round_no)
        log.debug("LLM 原始返回: %s", json.dumps(assistant_msg, ensure_ascii=False))
        if self.cancel_event.is_set():
            yield from self._tail_stopped(assistant_msg, usage_total, metrics)
            return
        yield from self._tail_answer(assistant_msg, usage_total, metrics,
                                     stopped_reason="max_rounds")

    # ------------------------------------------------------------------
    # 防失控提醒：死循环指纹 + 轮数预算（提醒不是砍停）
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_signature(name: str, raw_arguments) -> str:
        """一次工具调用的指纹 = sha1([工具名, 规范化参数])。

        arguments 是服务商给的 JSON 文本，键序/空白可能每次不同——先解析成
        对象再 sort_keys 序列化，语义相同的调用才得到同一个指纹；解析失败退回
        直接哈希原始文本。必须哈希而非原文：write_file 的参数可能带几万字符，
        明文留存是负担；指纹不写进日志。
        """
        try:
            canonical = json.dumps([name, json.loads(raw_arguments)],
                                   sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            canonical = json.dumps([name, str(raw_arguments)], ensure_ascii=False)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()

    def _inject_reminder(self, text: str, kind: str, detail: dict,
                         structural: bool = False) -> bool:
        """往历史里注入一条合成 user 提醒。默认受 MAX_TURN_REMINDERS 总预算约束，
        预算耗尽后一律放弃（提醒是提示性的，预算保证了它永远无法反过来绑架
        回合）。返回是否真正注入。

        structural=True：结构性提醒，不受总预算约束也不计数——用于"续轮"这类
        同时也是循环控制的一部分的提醒（自身已被 MAX_ROUND_EXTENSIONS 封顶，
        若再被提醒预算掐掉，会出现"上限已延长但模型不知道"的错位）。"""
        if not structural and self._reminders_used >= MAX_TURN_REMINDERS:
            return False
        if not structural:
            self._reminders_used += 1
        self.history.append({"role": "user", "_synthetic": True, "content": text})
        self.trace.append({"type": "system_reminder", "kind": kind, **detail})
        log.info("注入合成提醒（%s），本回合已用 %d/%d",
                 kind, self._reminders_used, MAX_TURN_REMINDERS)
        return True

    def _maybe_remind(self, tool_calls: list[dict], round_no: int,
                      denied_before: set | None = None) -> None:
        """工具结果入历史后检查三类提醒（静默进历史，不发任何事件——下一轮
        请求模型自然看到）：

        1. 循环提醒：按【请求顺序】逐个更新重复指纹 streak（与 _execute_tool_calls
           的回填顺序一致，论证见其 docstring「回填顺序只认请求顺序」）。同一
           签名连续达到 REPEAT_STREAK_REMIND 次才提醒，且同一段连续重复内只提醒
           一次（== 阈值才触发，第 4、5 次不再触发）；签名变化即重置计数。
        2. 被拒重试提醒（3b）：这一轮出现的调用，若其指纹在【本轮执行前】就已被
           权限拒绝过（denied_before，即上一轮被拒的调用），立即提醒——不等连续
           3 次。命中即从集合移除，避免同一条被拒调用反复触发。注意用
           denied_before 而非当前 _denied_sigs：后者含本轮刚被拒的调用，会把
           "被拒的同一轮"也误判成重试。
        3. 轮数预算提醒：round_no 进入预算提醒轮数集合（max_rounds-10 / -4 各
           一次）时提醒模型收敛。

        各类提醒共享 _reminders_used 预算（见 _inject_reminder）。
        """
        denied_before = denied_before or set()
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            sig = self._tool_signature(name, fn.get("arguments"))
            # 2. 被拒后原样重试：最高优先级，先判后销（一次即提醒）。
            if sig in denied_before:
                self._denied_sigs.discard(sig)
                self._inject_reminder(DENIED_REMIND_TEXT.format(name=name),
                                      "denied_retry", {"tool": name})
                self._streak_sig, self._streak_count = sig, 1
                continue
            if sig == self._streak_sig:
                self._streak_count += 1
            else:
                self._streak_sig, self._streak_count = sig, 1
            if self._streak_count == REPEAT_STREAK_REMIND:
                self._inject_reminder(REPEAT_REMIND_TEXT.format(
                    count=self._streak_count, name=name),
                    "repeat_loop", {"tool": name, "streak": self._streak_count})
        if round_no in self._budget_remind_rounds:
            self._inject_reminder(BUDGET_REMIND_TEXT.format(
                round_no=round_no, max_rounds=self.max_rounds),
                "round_budget", {"round": round_no})

    def chat(self, user_input: str, user_message: dict | None = None) -> str:
        """处理一次用户提问，返回最终文字回答（CLI 用；Web 走 run() 流式）。"""
        answer = ""
        for kind, payload in self.run(user_input, user_message):
            if kind == "done":
                answer = payload["answer"]
        return answer

    def reset(self) -> None:
        """清空对话历史，开始新会话。"""
        self.history.clear()

    # ------------------------------------------------------------------
    # 子代理（读侧扇出）：独立上下文的只读侦察员
    #
    # 调用链：模型请求 spawn_subagent → _run_tool → execute_tool →
    # ctx.subagent_runner（__init__ 自装配到这里）→ 本节的 _spawn_subagent。
    # 工具层只认 runner 签名，不 import 本模块——依赖保持单向（agent → tools
    # → subagent_tools）。
    # ------------------------------------------------------------------

    def run_attached(self, user_input: str, cancel_event: threading.Event):
        """附属模式跑一个回合：不创建、也不置位停止开关，直接复用调用方给的
        Event（生成器，事件与 run() 同名同形）。

        与 run() 的唯一差别是停止开关的生命周期：run() 每回合造新 Event 并在
        finally 里 set——那是父回合自有的开关，子代理照抄会把父回合一起杀掉；
        附属模式共享父开关，用户点「停止」时父子同时收场，且谁都不替谁置位。"""
        self.cancel_event = cancel_event
        yield from self._run(user_input)

    def _spawn_subagent(self, tasks: list[str]) -> str:
        """构造并同步驱动只读侦察子代理（tasks 数组），返回结论信封 JSON
        （spawn_subagent 的运行器，装配在 ctx.subagent_runner）。

        并行扇出：>1 个任务时交给线程池并发驱动，上限 SUBAGENT_MAX_PARALLEL
        （工具层已把关）。安全前提已逐项核实（论证见 subagent_tools.py 模块
        注释）：llm 客户端请求间无共享可变状态、子代理各自持有独立的
        ToolContext/history/闸门、工具面只读无共享写。线程池只在本次工具调用
        内存在，spawn_subagent 独占一个串行组，父循环此刻不做任何其它动作。

        与主代理的四个刻意差异：
          * 工具面：SUBAGENT_TOOLSET（只读侦察），spawn_subagent 不在其中——
            递归派生在 schema（模型看不见）与 _run_tool 名单闸两处被挡；
          * 轮数：SUBAGENT_MAX_ROUNDS 兜底，跑满走同一套禁工具收尾轮；
          * 权限：全新闸门 ask_timeout=0，但【继承用户规则加载器】——自定义
            deny/allow 规则必须对子代理同样生效，否则子代理成了绕过个性化
            禁令的旁路；ask 无卡可弹，按拒绝立即收场（安全侧）；
          * 事件：过程按【收窄集合】外发（round/tool_call/tool_result + 一条
            终态，载荷带 parent 标识，契约见 docs/protocol.md §4.3）——delta
            流（answer/reasoning）不外发：3 并发 × 十几轮的 delta 会刷穿 500
            条环形缓冲把 turn_start 挤掉，补发退化为"尽力补尾巴"；子代理的
            正文/思考对父时间线没有展示价值，工具轨迹足以讲清过程。历史与
            轨迹只落父回合自己的（子代理 history 随实例销毁，过程条目以
            sub_* 形态挂进父 trace 供回放/快照）。
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return error_result("父回合已停止，子代理未派出")
        if len(tasks) == 1:
            results = [self._run_one_subagent(tasks[0], 0)]
        else:
            with ThreadPoolExecutor(
                    max_workers=min(len(tasks), SUBAGENT_MAX_PARALLEL)) as pool:
                # futures 顺序 = 提交顺序 = 回填顺序：结果与任务的配对由下标
                # 保证，与哪个先跑完无关（与工具并行组同一条回填不变式）
                futures = [pool.submit(self._run_one_subagent, t, i)
                           for i, t in enumerate(tasks)]
                results = []
                for task, f in zip(tasks, futures):
                    try:
                        results.append(f.result())
                    except Exception as e:  # 单任务意外炸穿：只折损自己，不连坐同批
                        log.warning("子代理线程意外失败（task=%.40s）：%s", task, e)
                        results.append({"task": task, "ok": False,
                                        "error": f"子代理执行失败: {e}",
                                        "hint": "可缩小任务范围重试，或主代理自行侦察"})
        return json.dumps({"ok": True, "results": results}, ensure_ascii=False)

    def _run_one_subagent(self, task: str, index: int = 0) -> dict:
        """跑一个子代理，返回结果条目。本方法【不抛异常】：单个子代理的任何
        失败都折叠成自己的 error 条目，绝不连坐同批其它任务（并行时它跑在线程
        池工作线程上，与工具并行组的"单工具异常不连坐"同一纪律）。

        index 是本批任务的下标：子任务卡/事件的稳定序号（parent 才是归并键，
        index 只是展示序）。sub_id 在此生成并贯穿事件与 trace 条目。"""
        sub_id = uuid.uuid4().hex[:12]
        try:
            return self._run_one_subagent_inner(task, sub_id, index)
        except Exception as e:
            log.warning("子代理执行失败（task=%.40s）：%s", task, e)
            return {"task": task, "ok": False, "error": f"子代理执行失败: {e}",
                    "hint": "可缩小任务范围重试，或主代理自行侦察"}

    def _record_usage(self, kind: str, stats: dict) -> None:
        """把隐藏 LLM 消耗（子代理/压缩总结）交给用量归账 sink（app.py 注入 →
        db.record_usage 落 message_usage 独立行，kind 列区分来源）。未注入
        （CLI/单测）= 丢弃，与旧行为一致；无 usage 的调用不记账。sink 异常
        只记日志——归账是旁路观测，绝不打断回合。"""
        if self.usage_sink is None or not (stats.get("usage") or {}):
            return
        try:
            self.usage_sink(kind, stats)
        except Exception:
            log.exception("用量归账失败（忽略）")

    def _emit_sub_event(self, evt: dict) -> None:
        """子代理过程事件外发（event_sink，app.py 注入 = bus.publish + 快照）。
        sink 异常一律吞掉——嵌套 trace 是观测面，绝不能反过来影响子代理执行。"""
        sink = self.event_sink
        if sink is None:
            return
        try:
            sink(evt)
        except Exception:
            log.exception("子代理事件外发失败（忽略）")

    def _run_one_subagent_inner(self, task: str, sub_id: str, index: int) -> dict:
        child = Agent(llm=self.llm,
                      system_prompt=SUBAGENT_SYSTEM_PROMPT,
                      max_rounds=SUBAGENT_MAX_ROUNDS,
                      verbose=False,            # 子代理过程只进 agent.log，不刷父终端
                      vision_supported=False,   # 工具面里没有 analyze_image
                      workspace=self.ctx.workspace,
                      context_window=self.context_window,
                      permission_gate=PermissionGate(
                          self.ctx.workspace, ask_timeout=0.0,
                          user_rules_loader=self.permissions.user_rules_loader),
                      session_id=self.ctx.session_id,  # read_attachment 据此定位上传附件
                      model_tag=self.model_tag,
                      allowed_tools=SUBAGENT_TOOLSET)
        # 子代理不继承 result_sink：它的结果超长时走 60k 截断兜底——子代理
        # 没有 read_tool_result 工具（不在 SUBAGENT_TOOLSET），给了引用也无法
        # 读回，徒增一段"指着够不到"的提示。
        rounds, answer, stopped, usage = 0, "", False, {}
        trace_budget = SUB_TRACE_MAX_ENTRIES  # 每任务的过程条目预算（防塞爆父 trace）
        for kind, payload in child.run_attached(
                task, self.cancel_event or threading.Event()):
            if kind == "round":
                rounds = payload.get("round") or rounds
            elif kind == "done":
                answer = str(payload.get("answer") or "")
                stopped = bool(payload.get("stopped"))
                # done.usage 是子代理全程的累计口径（usage_total），权威取值
                usage = dict(payload.get("usage") or {})
            elif kind == "usage":
                # usage 事件是【单次请求】口径（与 done.usage 的累计不同）：
                # done 缺席（被停止/异常收场）时靠逐条累加兜住已烧掉的 token
                for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                    usage[key] = usage.get(key, 0) + (payload.get(key) or 0)
            # ── 嵌套 trace（①）：SSE 外发收窄集合 + 父 trace 挂 sub_* 条目 ──
            if kind in ("round", "tool_call", "tool_result"):
                self._emit_sub_event({"type": "subagent", "kind": kind,
                                      "parent": sub_id, "index": index,
                                      "task": task[:120], **payload})
            if kind in ("round", "tool_call", "tool_result"):
                # 预算按【实际入账的条目】扣（usage 事件不产条目、不占预算）
                if trace_budget > 0:
                    trace_budget -= 1
                    if kind == "round":
                        self.trace.append({"type": "sub_round", "parent": sub_id,
                                           "round": payload.get("round")})
                    elif kind == "tool_call":
                        self.trace.append({"type": "sub_tool_call", "parent": sub_id,
                                           "name": payload.get("name"),
                                           "arguments": payload.get("arguments")})
                    else:
                        self.trace.append({"type": "sub_tool_result", "parent": sub_id,
                                           "name": payload.get("name"),
                                           "result": payload.get("result")})
                elif trace_budget == 0:
                    trace_budget = -1
                    self.trace.append({"type": "sub_truncated", "parent": sub_id,
                                       "text": "…[子代理过程条目过多，已省略]"})
        # 停止判定必须在结论判定之前：被掐断的子代理走「手动停止」收尾，done
        # 里仍带着半截回答——那是残缺的中间产物，不是结论，回收它并继续跑
        # 父回合等于无视用户刚刚表达的「停下」。
        if stopped or (self.cancel_event and self.cancel_event.is_set()):
            entry = {"task": task, "ok": False,
                     "error": "子代理被用户停止，未回收结论",
                     "hint": "停止是全局的；需要继续侦察请在下一轮重新派出"}
        elif not answer.strip():
            entry = {"task": task, "ok": False,
                     "error": "子代理未产出结论（轮数耗尽且收尾轮为空）",
                     "hint": "缩小任务范围后重试，或主代理自行侦察"}
        else:
            report = answer[:SUBAGENT_REPORT_MAX_CHARS]
            if len(answer) > SUBAGENT_REPORT_MAX_CHARS:
                report += (f"\n…[报告超长已截断，共 {len(answer)} 字符；"
                           "需要细节请派边界更窄的子任务分次侦察]")
            entry = {"task": task, "ok": True, "report": report, "rounds": rounds,
                     # 子代理用量随信封透出（可观测）；独立归账走 _record_usage
                     "usage": {"prompt_tokens": usage.get("prompt_tokens") or 0,
                               "completion_tokens": usage.get("completion_tokens") or 0}}
        # 终态事件：前端子任务卡据此定格（✅ N 轮 / ❌ 原因）。ok 必须显式带上
        # ——前端按 evt.ok 真值分流，缺键会被误判成失败。
        done_evt = {"type": "subagent", "kind": "done", "parent": sub_id,
                    "index": index, "task": task[:120], "rounds": rounds,
                    "ok": bool(entry.get("ok"))}
        if entry.get("ok"):
            done_evt["report_head"] = entry["report"][:300]
        else:
            done_evt["error"] = entry.get("error") or ""
        self._emit_sub_event(done_evt)
        # 用量归账（②）：子代理全程（含被停止的半程）的真实消耗记一条
        # kind='subagent' 的 message_usage，供应商/模型随父（同一 llm 实例）
        self._record_usage("subagent", {"task": task[:200], "ok": entry.get("ok"),
                                        "rounds": rounds, "usage": usage})
        return entry

    # ------------------------------------------------------------------
    # 上下文压缩
    # ------------------------------------------------------------------

    @staticmethod
    def _compact_split(view: list[dict], head: int,
                       keep: int = COMPACT_KEEP_TAIL, min_segment: int = COMPACT_MIN_SEGMENT) -> int | None:
        """在模型视图里选压缩切点：返回 cut，view[head:cut] 将被总结、view[cut:] 原样保留。

        切点必须落在"完整的对话回合之间"，两条规则（违反任意一条，之后的请求
        直接被服务商 400 拒收，这是压缩实现里最经典的暗坑）：
        1. view[cut] 不能是 role=tool——否则保留段以"没有对应调用的工具结果"开头；
        2. view[cut-1] 不能是带 tool_calls 的 assistant——否则被压缩段以
           "悬空的工具调用请求"结尾。两条都不满足就左移切点再试。

        head 是视图中第一条"可压缩"消息的下标（首条用户消息之后；之前压缩过
        再压时，旧摘要也在可压缩段内，会被并入新摘要）。
        中段凑不够 min_segment 条返回 None：刚压完又触发说明窗口实在太小，
        硬压只会死循环，放弃更安全（调用方打日志后跳过，下轮再看）。
        """
        cut = len(view) - keep
        while cut >= head + min_segment:
            m, prev = view[cut], view[cut - 1]
            if m.get("role") != "tool" and not (prev.get("role") == "assistant" and prev.get("tool_calls")):
                return cut
            cut -= 1
        return None

    @staticmethod
    def _transcript(messages: list[dict]) -> str:
        """把一段消息序列转成纯文本记录，供总结调用使用。

        为什么不把消息原样发给总结模型：中段的开头/结尾是随意截断的边界，
        原样发出去稍不留神就撞上 tool_call/tool_result 配对校验；拍平成带
        角色标签的文本则对任何服务商都合法，模型读"会议纪要"的能力也足够好。
        图片部分无法（也不该）进纯文本，跳过。
        """
        lines = []
        for m in messages:
            label = {"user": "用户", "assistant": "助手"}.get(m.get("role"), "工具结果")
            parts = []
            content = m.get("content")
            if isinstance(content, list):  # 多部分消息（带图附件）：只留文字
                content = "\n".join(str(p.get("text") or "") for p in content
                                    if isinstance(p, dict) and p.get("type") == "text")
            if content:
                parts.append(str(content))
            for call in m.get("tool_calls") or []:  # 工具调用请求是历史的骨架，必须进纪要
                fn = call.get("function") or {}
                parts.append(f"[调用工具 {fn.get('name')} 参数 {fn.get('arguments')}]")
            lines.append(f"—— {label} ——\n" + ("\n".join(parts) if parts else "（无正文）"))
        return "\n\n".join(lines)

    @staticmethod
    def _is_error_envelope(content: str) -> bool:
        """工具结果是否为失败信封（{ok:false,…} 或带 error 键的 JSON 对象）。

        按信封语义判断而非子串匹配（'"error" in content' 会把正文里恰好引用
        了 error 字样的成功结果误判成错误，比如 grep 命中错误处理代码）。宽
        容错口径：解析不出 JSON 的历史内容一律当非错误——清掉它无损，误豁免
        反而让大块可清的内容一直留着。"""
        try:
            info = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
        return isinstance(info, dict) and (info.get("ok") is False or "error" in info)

    def _clear_old_tool_results(self, keep_recent: int = CLEAR_TOOL_RESULTS_KEEP_RECENT) -> int:
        """分级压缩第一档（无损、便宜）：把较早的工具结果正文换成一行占位符。

        为什么值得单独一档：工具结果（整文件内容、命令输出、搜索命中）通常是
        上下文里最占地方的部分，而它们的价值随轮次迅速衰减。清掉它们**不丢
        任何决策信息**（任务目标、改动清单都还在），比动摘要安全得多，也不花
        一次 LLM 调用。所以超阈值时先做这一档，清完仍超才去摘要。

        做法：只换 content，**保留消息骨架**（role / tool_call_id / 顺序）。
        绝不能删消息——tool 消息与前面 assistant.tool_calls 配对，删了会出现
        "没有请求却冒出结果"的悬空消息，服务端直接 400。

        返回：实际清理的条数（0 = 没动，交给上层去摘要）。
        本方法只改内存里的 self.history。落库口径要如实说：被清理的消息内容
        变了 → 内容指纹变了 → 下一次 save_messages 会把占位符重写进对应行
        （/_tool_result_cleared 标记同样随行进库）。也就是说清理是【全链路】
        的——DB 与前端时间线回放里，较早的工具结果同样显示占位符。这是有意
        的取舍：工具输出的价值随轮次衰减，占位符里写了"如何重新获取"；
        换来的是之后每一轮请求都实打实少带几万字符。
        """
        # 先找出所有"可清理"的工具结果下标：跳过已被摘要吸收的（边界之前）——
        # 那些本来就不在模型视图里，清了也白清，还会误改 DB 待写的行。
        last_boundary = -1
        for i, m in enumerate(self.history):
            if m.get("role") == "compact":
                last_boundary = i
        tool_idx = [i for i, m in enumerate(self.history)
                    if i > last_boundary and m.get("role") == "tool"]
        if len(tool_idx) <= keep_recent:
            return 0  # 工具结果还不够多，清了也省不了多少

        targets = tool_idx[:-keep_recent]  # 除最近 keep_recent 条外全清
        # 先只统计能省多少，够本了才真动手——避免"清了又回滚"改坏原内容。
        saved = 0
        plan = []  # [(下标, 原正文)]
        for i in targets:
            m = self.history[i]
            content = m.get("content")
            if not isinstance(content, str) or not content:
                continue  # 已经是占位符/空：跳过（幂等，可反复调用）
            if content == CLEARED_TOOL_RESULT_PLACEHOLDER:
                continue
            if self._is_error_envelope(content):
                # 豁免白名单：失败结果不清。错误信息（含权限拒绝）是模型判断
                # "此路不通、换道"的依据，清掉它，模型再遇同类场景会原样重踩；
                # 且错误结果通常很短，清了也省不了多少。按信封语义判断而非
                # 子串匹配：正文里恰好引用了 "error" 字样的成功结果（grep 命中
                # 错误处理代码等）不该被误豁免。
                continue
            saved += len(content)
            plan.append((i, content))

        if saved < CLEAR_TOOL_RESULTS_MIN_SAVING:
            return 0  # 省的还不够塞牙缝，不值得动（保护原始内容）

        cleared = 0
        for i, _orig in plan:
            m = self.history[i]
            m["content"] = self._cleared_placeholder(_orig)
            m["_tool_result_cleared"] = True  # 标记（下划线前缀，发给模型前会被剥离）
            cleared += 1

        if cleared:
            # 清理改变了字符总量 → 校准系数作废（与压缩同一理由：系数是按
            # 原始字符量校准的，内容换了密度就变了）。下轮真实 usage 重新校准。
            self._token_ratio = None
            log.info("分级压缩①：清理 %d 条较早工具结果（省约 %d 字符，保留最近 %d 条）",
                     cleared, saved, keep_recent)
        return cleared

    def _maybe_compact(self, force: bool = False) -> dict | None:
        """回答结束后调用：估算超阈值就把中段历史压缩成一条边界标记。

        分级：超阈值先试**无损**的"清旧工具结果"（_clear_old_tool_results），
        清完重新估算；仍超阈值才做**有损**的摘要。这样能省下不少"本可不必
        摘要"的场景——工具输出往往是上下文大头，清掉它经常就够了。

        熔断：摘要调用连续失败 MAX_COMPACT_FAILURES 次（网络/余额/服务商
        故障）后停止自动压缩——每次失败都让回合收尾白等一次超时；成功一次
        即清零。被用户停止掐断的总结不算失败（不是服务商的错）。

        force=True 供 /compact 手动触发：跳过窗口/熔断/阈值三道自动闸（用户
        点名要压缩，清完旧工具结果后无论是否已降到阈值以下都继续摘要），
        但保留"没有可安全压缩的段落"的保护（历史太短时切不出切段就放弃）。

        返回给前端的事件载荷（未触发/失败返回 None，原因记在
        _compact_skip_reason 供手动触发时反馈）。任何异常都不往外抛——
        压缩是"锦上添花"，绝不能让它打断会话；失败就跳过，下一轮回答结束后
        阈值依然超着，自然会重试。
        """
        self._compact_skip_reason = None
        if not force and not self.context_window:
            self._compact_skip_reason = "未配置上下文窗口，自动压缩关闭"
            return None
        if not force and self._compact_fail_streak >= MAX_COMPACT_FAILURES:
            self._compact_skip_reason = f"连续压缩失败 {self._compact_fail_streak} 次，已熔断"
            log.warning("上下文压缩已连续失败 %d 次，熔断暂停自动压缩（本会话内）",
                        self._compact_fail_streak)
            return None
        stats = self.context_stats()  # 校准系数可用则校准，否则 CJK 感知估算
        est_before = sum(stats.values())
        # 触发线 = 窗口的 80%（能力口径：估算有误差，给输出留余量），再与
        # COMPACTION_TARGET_TOKENS（成本口径，env 可选）取较小者。窗口是
        # "模型能吃多少"，成本线是"愿意为单次请求的历史付多少"——大窗口模型
        # 配小成本线，历史瘦身更勤而不牺牲单轮能力；0/未设置 = 关闭，维持纯
        # 窗口口径（现网默认）。force 时无窗口则阈值为 0（比较被跳过，仅日志用）。
        threshold = (self.context_window or 0) * COMPACT_THRESHOLD
        try:
            target = int(os.environ.get("COMPACTION_TARGET_TOKENS") or 0)
        except ValueError:
            target = 0
        if target > 0:
            threshold = min(threshold, target)
        if not force and est_before <= threshold:
            return None

        # ── 分级压缩第一档：先清较早的工具结果（无损、不花 LLM 调用）──
        # 清完重新估算；降到阈值以下就直接收工，不必动摘要——省下一次有损
        # 总结，也省一次 LLM 调用。前端不产卡片（没有语义损失，无需告知）。
        # force 时只清不判：用户点名压缩，摘要这一步一定要走。
        if self._clear_old_tool_results():
            stats = self.context_stats()
            est_after_clear = sum(stats.values())
            if not force and est_after_clear <= threshold:
                log.info("分级压缩①后已降至阈值以下（估算 %d → %d tokens），跳过摘要",
                         est_before, est_after_clear)
                return None

        view = self._messages_for_model()
        first_user, last_boundary = -1, -1
        for i, m in enumerate(self.history):
            if m.get("role") == "compact":
                last_boundary = i
            elif first_user < 0 and m.get("role") == "user":
                first_user = i
        if first_user < 0:
            self._compact_skip_reason = "历史里还没有用户消息"
            return None
        # 视图结构：[首条用户消息] + [旧摘要(若有)] + [活区消息]。
        # head = 活区在视图里的起始下标（第一条"可压缩"消息）；无边界时视图就是
        # 原始 history 本身，活区从首条用户消息之后开始。
        has_boundary = last_boundary >= 0
        head = 2 if has_boundary else first_user + 1
        cut = self._compact_split(view, head)
        if cut is None:
            self._compact_skip_reason = "没有可安全压缩的段落（历史太短，或刚压缩过）"
            log.warning("上下文估算 %d tokens 超过阈值 %d，但没有可安全压缩的段落，跳过",
                        est_before, threshold)
            return None

        # 总结输入必须带上旧摘要（视图下标 1，不在 head 起的活区映射里）：再压缩
        # 时"最早一段"的原文只剩旧摘要这一份转述，不并入新摘要就会随旧边界失效
        # 凭空消失（_compact_split docstring 承诺的"旧摘要并入新摘要"在这里兑现；
        # 切点与插入位置仍按 head 算，视图下标映射不受影响）。
        transcript = self._transcript(view[(1 if has_boundary else head):cut])
        request = [{"role": "user",
                    "content": f"{SUMMARIZE_PROMPT}\n\n===== 待压缩的对话记录开始 =====\n"
                               f"{transcript}\n===== 待压缩的对话记录结束 =====\n请输出摘要："}]
        log.info("上下文压缩：估算 %d tokens 超阈值，总结 %d 条消息（视图下标 %d..%d）…",
                 est_before, cut - head, head, cut - 1)
        reply = None
        compact_usage = {}
        try:
            # 复用会话同一个 LLM 与停止开关（chat_stream 带看护线程，用户等不及点
            # 停止也能掐断这次总结）；delta 片段直接丢弃——压缩不产生回答流。
            for kind, payload in self.llm.chat_stream(messages=request, cancel=self.cancel_event):
                if kind == "message":
                    reply = payload
                elif kind == "usage":
                    compact_usage = payload  # 单次请求的用量，最后一次即全程
        except Exception as e:  # 网络/服务商错误：计一次失败（熔断用），本轮跳过
            self._compact_fail_streak += 1
            self._compact_skip_reason = f"压缩调用失败：{e}"
            log.warning("上下文压缩失败（%s），连续第 %d 次；将在下一轮回答结束后重试（达 %d 次熔断）",
                        e, self._compact_fail_streak, MAX_COMPACT_FAILURES)
            return None
        summary = ((reply or {}).get("content") or "").strip()
        if not summary or self.cancel_event.is_set():
            self._compact_skip_reason = "总结为空或被停止掐断"
            return None  # 被停止掐断的半截总结不可信，作废重来（不算服务商失败）

        marker = {
            # 特殊 role：DB/前端按普通消息存取和回放；模型视图里被 _visible_history
            # 替换成上面的摘要 user 消息，永远不会原样发给模型。
            "role": "compact",
            "content": summary,
            "is_compact_boundary": True,  # 边界标记元数据（前端据此渲染分隔卡片）
            "_stats": {"compacted": True, "est_tokens": est_before},
        }
        # 视图下标 → 原始历史下标：活区在视图里从 head 起、在 history 里从 live_start
        # 起（同一段对象一一对应），边界插到"被压缩段的最后一条"与"保留段的第一条"之间。
        live_start = (last_boundary + 1) if has_boundary else (first_user + 1)
        insert_at = live_start + (cut - head)
        self.history.insert(insert_at, marker)
        self._compact_fail_streak = 0  # 成功即清零熔断计数
        # 压缩后文件重注入：被摘要吸收的"最近读过的文件"以合成消息重放——
        # 模型不必盲目重读就能继续改代码。预算内装不下的降级为一行引用。
        # _synthetic 生命周期同收尾指令：发给模型、不落库、不进提取输入。
        reminder = self._build_post_compact_reminder(
            tail_text="".join(str(m.get("content") or "") for m in self.history[insert_at + 1:]),
            reads=self.recent_reads)
        if reminder:
            self.history.append({"role": "user", "_synthetic": True, "content": reminder})
        # 校准系数作废：它是在"原始历史"的字符总量上校准的，压缩后请求里换成
        # 了摘要（token 密度完全不同），旧系数会把估算带偏；置回 None 让
        # context_stats 退回 CJK 感知估算，等下一轮真实 usage 到达再重新校准。
        self._token_ratio = None

        stats_after = self.context_stats()
        log.info("上下文压缩完成：估算 %d → %d tokens（保留最近 %d 条，摘要 %d 字）",
                 est_before, sum(stats_after.values()), len(view) - cut, len(summary))
        # 用量归账（②）：压缩总结这次隐藏 LLM 调用的消耗记一条 kind='compact'
        # 的 message_usage——不记的话用量页永远比账单少这一块。
        self._record_usage("compact", {"est_tokens": est_before,
                                       "summary_chars": len(summary),
                                       "usage": compact_usage})
        self._compact_skip_reason = None
        return {"summary": summary, "prompt_tokens": sum(stats_after.values()), "context": stats_after}

    def compact_now(self) -> dict:
        """手动触发一次压缩（/compact 斜杠命令）。

        走 _maybe_compact 的 force 路径：不看阈值/熔断/窗口，但保留"没有可
        安全压缩的段落"保护。返回统一载荷：compacted=True 时带 summary/
        prompt_tokens/context（与自动压缩事件同形，前端处理可复用）；
        compacted=False 时带 reason（取自 _compact_skip_reason）。
        注意：本方法只改内存历史，落盘与 SSE 事件由调用方（app.py）负责——
        与回合收尾的分工一致（agent 不碰存储层）。
        """
        if self.cancel_event is None:
            # 从未跑过回合的会话（服务重启后恢复、没发过消息）：run() 还没机会
            # 创建停止开关。给一个全新的 Event——is_set() 恒 False，压缩不被掐。
            self.cancel_event = threading.Event()
        payload = self._maybe_compact(force=True)
        if payload is None:
            return {"ok": True, "compacted": False,
                    "reason": self._compact_skip_reason or "没有可压缩的内容"}
        return {"ok": True, "compacted": True, **payload}

    @staticmethod
    def _build_post_compact_reminder(tail_text: str,
                                     reads: list[tuple[str, str]] | None = None) -> str:
        """构造压缩后的文件重注入消息（纯函数）。

        输入 reads 是 [(路径, read_file 当时返回的带行号片段)]，最旧在前。
        规则：只取最近 REINJECT_MAX_FILES 个、内容仍【不在】保留段里的文件
        （还在原文里的不需要重注入）；单文件超 REINJECT_FILE_CHARS 已在记录
        时截过，此处再控总量 REINJECT_TOTAL_CHARS——装不下的降级为一行引用，
        提示模型需要时重新 read_file。全部装不下/没有记录时返回空串。
        """
        items = list(reversed(reads if reads is not None else []))  # 最新在前
        seen_paths: set[str] = set()
        parts: list[str] = []
        used = 0
        overflow: list[str] = []
        for path, snippet in items:
            if path in seen_paths:  # 同一文件多次读：只看最近一次
                continue
            seen_paths.add(path)
            if len(parts) >= REINJECT_MAX_FILES:
                overflow.append(path)
                continue
            if snippet[:200] and snippet[:200] in tail_text:
                continue  # 内容还在保留段原文里：跳过
            header = f"### {path}\n"
            budget = REINJECT_TOTAL_CHARS - used - len(header)
            if budget <= 200:  # 剩余空间装不下有意义的内容：降级为引用
                overflow.append(path)
                continue
            body = snippet[:budget]
            used += len(header) + len(body)
            parts.append(header + body)
        for path in overflow:
            parts.append(f"### {path}（内容过长未注入，需要时重新 read_file）")
        if not parts:
            return ""
        return ("【上下文恢复】更早的对话已压缩为摘要。以下是压缩前最近读取的文件内容"
                "（可能是旧版本，动手修改前请先重新 read_file 核对）：\n\n"
                + "\n\n".join(parts))

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 工具执行：读并行 / 写串行（同一轮 tool_calls 的分组调度）
    # ------------------------------------------------------------------

    def _execute_tool_calls(self, tool_calls: list[dict]):
        """执行同一轮的全部 tool_calls，按请求顺序逐个产出 ("tool_result", ...) 事件。

        ── 阶段〇：权限闸门（本方法新增的前置阶段）──
        分组调度之前，先在【当前线程】（回合线程，即 app.py 的 worker）对本轮
        全部 tool_calls 逐个判定。判定与等待绝不能落进 ThreadPoolExecutor 的
        工作线程，三段论：
        1. 并行组内没有暂停点——线程池的 future 一旦提交就必须跑到结束，
           组内没有任何"挂起等用户"的位置；
        2. 在工作线程里等用户会卡死线程池并破坏组间屏障——ask 一等几分钟，
           4 个 worker 全被占住，后续所有组（哪怕全是只读工具）无限排队；
           且屏障的语义是"上一组全部结束才进下一组"，永远结束不了的组
           直接把调度管线焊死；
        3. 恢复后必须对整轮重新分组调度——用户决定会改变每个调用的
           allow/deny 状态，也会写入会话记忆（「本会话内同类操作不再问」）；
           只有拿最终状态重新分组，才能既保住"连续只读才并行"的组 formation，
           又保住「回填顺序 = 请求顺序」不变式（见下文 3，论证不变）。
        含 ask 时：逐条产出 ("permission_request", {id, tool, input, reason})
        事件（app.py 经事件总线推给前端弹确认卡片），然后 wait_all 阻塞在
        【回合线程】——HTTP 线程与池线程都不受影响；用户在
        POST /api/sessions/<sid>/permission/<id> 里的决定通过 Event 唤醒。
        超时/停止一律按拒绝收场（超时不是安全边界，只是防挂死）。

        调度规则：tool_calls 序列被切成若干组——【连续的只读工具】为一组，
        交给线程池并行执行；每个非只读（写）工具单独成组，在当前线程串行
        执行。进入下一组前必须拿到上一组的全部结果（收集处即屏障）。
        deny 的调用不执行、不参与分组，在它的请求位置原位回填带原因的拒绝
        结果（它是即时回填、不产生执行，不会扰动读写顺序语义）。

        为什么"读写分组"能保证顺序安全（效果等价于纯串行执行）：
        1. 组内并行不改变任何结果：read_only 工具对工作区和会话状态零写入
           （read_file / list_dir / grep 只打开文件读，todo_write 只写
           ToolContext 内存），彼此没有数据依赖——谁先谁后执行，各自的输出
           都一样。并行只是把总耗时从"各调用相加"变成"取最慢者"；
        2. 组间屏障保住读写顺序：非只读工具会改工作区状态（写文件/改代码/
           跑命令），它与前后的调用存在真实依赖——写之前的读组必须全部
           完成（不能读到"尚未发生的写"），写之后的调用必须等写落地（才能
           读到它写入的结果）。按组顺序推进，读写之间、写写之间的相对顺序
           就与模型请求顺序严格一致，不会出现两个写并行互踩、或读穿越到
           写的另一侧；
        3. 回填顺序只认请求顺序、不认完成顺序：并行组的 futures 按提交顺序
           收集，结果下标与调用下标一一对应；主线程再按原顺序 append 历史、
           yield 事件。历史里 tool 消息的顺序因此与 assistant 消息里
           tool_calls 的顺序完全相同——服务商按 tool_call_id 配对、模型按
           顺序引用结果，任何错位都会把结果安到别的调用头上。
        """
        # ── 阶段〇：权限判定（回合线程里、分组调度之前）──
        # arguments 在这里解析一次供判定用；_run_tool 执行时还会自己解析
        # （它要容错畸形 JSON），两处互不依赖。
        items: list[dict] = []  # [{call, name, args, verdict}]
        for call in tool_calls:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            items.append({"call": call, "name": fn.get("name", ""),
                          "args": args,
                          "verdict": self.permissions.check(fn.get("name", ""), args)})
        if any(it["verdict"].verb == ASK for it in items):
            # ask：登记待确认（同一规则去重，一轮只弹一张卡）→ 发事件 →
            # 等决定 → 用一次性 overlay + 会话记忆对整轮重新判定。
            requests = self.permissions.open_requests(
                [(it["name"], it["args"], it["verdict"])
                 for it in items if it["verdict"].verb == ASK])
            for req in requests:
                self._log(f"🔐 等待权限确认: {req['tool']} {req['reason']}", "yellow")
                yield "permission_request", req
            decisions = self.permissions.wait_all(cancel=self.cancel_event)
            overlay = self.permissions.apply_decisions(decisions)
            for it in items:
                it["verdict"] = self.permissions.check(it["name"], it["args"],
                                                       overlay=overlay)
                if it["verdict"].verb == ASK:
                    # 理论不可达：open_requests 为每条 ask 登记了请求，overlay
                    # 必然覆盖它的键。留这道闸是防御规则键错位——残留的 ask
                    # 按拒绝收场（安全侧），绝不能掉进下面的执行循环。
                    it["verdict"] = Verdict(DENY,
                                            f"{it['verdict'].reason}；确认状态丢失，按拒绝处理")

        # ── 分组调度：只在 allow 的调用上成立；deny 原位回填拒绝结果 ──
        idx = 0
        while idx < len(items):
            it = items[idx]
            if it["verdict"].verb == DENY:
                self._log(f"⛔ 权限拒绝: {it['name']} {it['verdict'].reason}", "yellow")
                # 记下被拒指纹：下一轮 _maybe_remind 若又见到同一调用，立即提醒
                # 模型别原样重试（3b，拦截白等一个权限确认超时）。
                fn = it["call"].get("function") or {}
                self._denied_sigs.add(self._tool_signature(
                    fn.get("name", ""), fn.get("arguments")))
                yield self._backfill_tool_result(it["call"], rejection_result(it["verdict"]))
                idx += 1
                continue
            end = idx
            while (end < len(items) and items[end]["verdict"].verb == ALLOW
                   and is_read_only(items[end]["name"])):
                end += 1
            if end == idx:  # 首个 allow 是写操作：单独成组，当前线程串行执行
                end = idx + 1
            group = [items[i]["call"] for i in range(idx, end)]
            for call, result in zip(group, self._run_tool_group(group)):
                yield self._backfill_tool_result(call, result)
                # 「实质进展」判定（续轮依据，见 _run 主循环）：本组只要有一个
                # 非只读工具成功执行（结果无 error），就认为这一轮在推进任务。
                # 只读侦察不算、失败/被拒不算——防"读来读去原地打转"骗续轮。
                name = (call.get("function") or {}).get("name", "")
                if not is_read_only(name) and '"error"' not in result:
                    self._round_had_progress = True
                # create_doc 成功：额外产出一条 doc_created 事件（走 app.py 的
                # else 分支进 SSE 总线），前端据此自动弹出右侧文档面板。
                # 失败（result 含 error）不推——没生成成功没什么可弹的。
                # todo_write 成功：额外产出 todo_update 事件（清单已存 ctx.todos），
                # 前端据此在过程面板上方渲染任务清单卡（进度一目了然）。
                if name == "todo_write" and '"error"' not in result:
                    try:
                        info = json.loads(result)
                        if info.get("ok"):
                            yield "todo_update", {"todos": info.get("todos") or []}
                    except (json.JSONDecodeError, TypeError):
                        pass
                if name == "create_doc" and '"error"' not in result:
                    try:
                        info = json.loads(result)
                        if info.get("ok") and info.get("name"):
                            yield "doc_created", {"name": info["name"]}
                    except (json.JSONDecodeError, TypeError):
                        pass
            idx = end

    # ------------------------------------------------------------------
    # 结果落盘轻引用：超内联阈值的工具结果全文落盘，历史里只留头部预览 +
    # full 引用（read_tool_result 工具按行分段读回）。
    # ------------------------------------------------------------------

    def _externalize_tool_result(self, result: str) -> str:
        """结果进历史前的最后一道处理（_backfill_tool_result 唯一入口）。

        三档：
        1. ≤ TOOL_RESULT_EXTERNALIZE_CHARS：原样（绝大多数结果）；
        2. 超阈值且有 result_sink：全文落盘，历史里换成"预览 + 落盘提示 +
           full 引用"。JSON 信封（统一失败/成功信封）保留 ok/error/hint 等
           语义键、只截 result 字段正文——错误信息是模型改道的依据，绝不能
           因截断丢失；非信封内容（纯文本）直接预览 + 落盘提示；
        3. 超阈值且无 sink（CLI/单测）：退回旧的 60k 截断行为。

        返回值只可能是"更短或等长"的历史内容——本函数是上下文瘦身闸，任何
        分支都不得让历史内容比原结果更长。落盘失败按无 sink 处理（退截断），
        绝不让存储问题打断工具链路。
        """
        if len(result) <= TOOL_RESULT_EXTERNALIZE_CHARS:
            return result
        # 落盘的是"要读的正文"而不是 JSON 信封壳：信封是单行超长 JSON（正文
        # 以 \n 转义挤在里面），read_tool_result 按行分页对它退化成"一页一行
        # 转义串"。信封带 result 字段时落盘其正文（多行可读、天然可分页）；
        # ok/error/hint 语义键已保留在历史信封里，不随盘丢失。
        try:
            info = json.loads(result)
        except (json.JSONDecodeError, ValueError, TypeError):
            info = None
        envelope = info if isinstance(info, dict) and isinstance(info.get("result"), str) else None
        stored = envelope["result"] if envelope else result
        ref = None
        if self.result_sink is not None:
            try:
                ref = self.result_sink(stored)
            except Exception as e:
                log.warning("工具结果落盘失败（退回截断）：%s", e)
        if not isinstance(ref, dict) or not ref.get("path"):
            ref = None
        if ref is None:
            if len(result) <= MAX_TOOL_RESULT_CHARS:
                return result
            keep = MAX_TOOL_RESULT_CHARS
            self._log(f"✂️ 工具结果超长，已截断至 {keep} 字符", "yellow")
            return (result[:keep]
                    + f"\n…[工具结果过长（共 {len(result)} 字符），已截断至前 {keep} 字符。"
                      "需要余下内容请用更窄的参数分段读取（offset/limit、grep、head 等），"
                      "不要重试同样的大范围读取]")
        note = (f"\n…[结果过长（共 {ref['chars']} 字符），已内联前 "
                f"{TOOL_RESULT_PREVIEW_CHARS} 字符，完整原文已落盘：{ref['path']}。"
                f"需要更多内容请调用 read_tool_result（ref=\"{ref['path']}\"，支持 "
                "offset/limit 行分段），不要重试同样的大范围读取]")
        if envelope is not None:
            # 统一信封：语义键原样保留，只截 result 字段正文
            envelope["result"] = envelope["result"][:TOOL_RESULT_PREVIEW_CHARS] + note
            envelope["full"] = {"path": ref["path"], "bytes": ref["bytes"],
                                "chars": ref["chars"]}
            out = json.dumps(envelope, ensure_ascii=False)
        else:
            out = result[:TOOL_RESULT_PREVIEW_CHARS] + note
        self._log(f"📄 工具结果 {len(result)} 字符已落盘（{ref['path']}），历史内联预览",
                  "yellow")
        return out

    @staticmethod
    def _cleared_placeholder(content: str) -> str:
        """清理较早工具结果时的占位文案（_clear_old_tool_results 用）。

        带 full 引用的结果升级为"指针占位符"：正文虽被清理，完整原文仍在盘上，
        模型用 read_tool_result 按需读回即可——被清理的结果从"必须重调工具
        重新获取"变成"引用还在、随取随读"。这是轻引用最大的收益点之一。
        """
        try:
            info = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            info = None
        full = info.get("full") if isinstance(info, dict) else None
        if isinstance(full, dict) and full.get("path"):
            return (f"[较早的工具结果已清理以节省上下文；完整原文仍在盘上"
                    f"（{full['path']}"
                    + (f"，共 {full['chars']} 字符" if full.get("chars") else "")
                    + f"），可用 read_tool_result（ref=\"{full['path']}\"）分段读取]")
        return CLEARED_TOOL_RESULT_PLACEHOLDER

    def _backfill_tool_result(self, call: dict, result: str):
        """把一个工具结果按请求位置回填：追加历史、记轨迹、产出事件。
        正常执行与权限拒绝共用同一条回填路径——对下游（历史配对/前端渲染）
        而言，拒绝结果就是一个普通的（带 error 的）工具结果。"""
        result = self._externalize_tool_result(result)
        tool_msg = {
            "role": "tool",
            "tool_call_id": call.get("id", ""),  # 与请求里的 id 对应，服务商靠它配对
            "content": result,
        }
        self.history.append(tool_msg)
        self._log(f"🔧 工具返回: {result}", "yellow")
        name = (call.get("function") or {}).get("name", "")
        self.trace.append({"type": "tool_result", "name": name, "result": result})
        return ("tool_result", {"name": name, "result": result})

    def _run_tool_group(self, group: list[dict]) -> list[str]:
        """执行一组 tool_calls，返回与 group 下标一一对应的结果列表。

        只有 ≥2 个调用才开线程池：并行的收益是"耗时相加变取最慢"，单个
        调用开池纯属浪费（还多一次线程创建与切换）。写工具永远只会以
        大小为 1 的组走到这里，天然串行。
        """
        if len(group) < 2:
            return [self._run_tool(group[0])]
        names = ", ".join(c["function"].get("name", "?") for c in group)
        self._log(f"⚡ 只读工具并行执行（{len(group)} 个）: {names}", "cyan")
        with ThreadPoolExecutor(max_workers=PARALLEL_TOOL_WORKERS) as pool:
            # futures 列表顺序 = 提交顺序 = 回填顺序：结果与调用的配对由
            # 下标保证，与哪个先跑完无关
            futures = [pool.submit(self._run_tool, c) for c in group]
            results = []
            for f in futures:
                try:
                    results.append(f.result())
                except Exception as e:
                    # 双保险：_run_tool 承诺不抛（见其 docstring），万一真抛了，
                    # 也只把这一个调用转成 error 回填，绝不让整组连坐
                    results.append(error_result(f"{type(e).__name__}: {e}", "工具内部异常，可换用其它工具或稍后重试"))
            return results

    def _run_tool(self, call: dict) -> str:
        """解析并执行一次工具调用，任何错误都转成字符串交给 LLM 处理。

        本方法【绝不抛异常】：串行路径靠它把错误反馈给模型；并行路径里它
        跑在 ThreadPoolExecutor 的工作线程上，一旦抛出，future.result() 会在
        收集处重新抛出、殃及同组其它工具的回填（要求：单个工具出错不能
        影响同组其它工具）。

        参数解析失败（非法 JSON / 不是 JSON 对象）时不执行工具、直接回错误
        信封并附上该工具的期望参数定义（schema）——模型看到的不再是笼统的
        "参数不匹配"，同一轮就能对照修正重试；此前静默换成 {} 继续执行，
        模型会把"JSON 写坏"误诊为"字段名记错"，白白多烧一轮。

        钩子时序：pre 在解析之前（否决连解析都不必发生）；post 只对【真实
        执行过】的调用运行（解析失败/否决/执行抛异常都不跑 post）——post
        的输入必须是工具的真实产物，改写才有意义。
        """
        name = (call.get("function") or {}).get("name", "")
        raw_args = (call.get("function") or {}).get("arguments") or "{}"

        # 白名单前置闸（子代理防幻觉调用）：schema 已过滤，模型正常情况下看
        # 不到名单外的工具；这里仍拦一道，把"幻觉出的调用"变成可读的错误信封
        # 而不是执行到注册表里（execute_tool 按全量表查找，白名单限制会失守）。
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return error_result(f"本代理无权使用工具 {name}",
                                "只能使用任务说明给出的只读侦察工具；需要写操作请写进结论，"
                                "由主代理决定执行")

        # ── pre 钩子：返回拒绝原因字符串即否决 ─────────────────────────
        for hook in self.pre_tool_hooks:
            try:
                reason = hook(name, raw_args, self.ctx)
            except Exception:
                log.exception("pre_tool_hook 执行失败（忽略）")
                continue
            if isinstance(reason, str) and reason:
                return error_result(f"工具调用被拦截: {reason}",
                                    "本次调用未执行。调整方式后重试，或改用其它工具。")

        def args_error(reason: str) -> str:
            payload = {"ok": False, "error": reason,
                       "hint": "arguments 是【JSON 字符串】，修正后原样重试本工具"}
            schema = tool_schema(name)
            if schema:
                payload["schema"] = schema
            return json.dumps(payload, ensure_ascii=False)

        result: str | None = None
        executed_args: dict | None = None
        try:
            # 注意坑点：arguments 是【JSON 字符串】不是 dict（模型输出的是文本）
            arguments = json.loads(raw_args)
            if not isinstance(arguments, dict):
                result = args_error(f"arguments 必须是 JSON 对象（{{\"参数\": 值}})，实际是 "
                                    f"{type(arguments).__name__}；原文开头: {raw_args[:200]}")
        except json.JSONDecodeError as e:
            result = args_error(f"arguments 不是合法 JSON: {e}；原文开头: {raw_args[:200]}")
        if result is None:
            try:
                result = execute_tool(name, arguments, self.ctx)
                executed_args = arguments
            except Exception as e:  # execute_tool 已兜底一次；这里再兜一层，守住"绝不抛"的承诺
                result = error_result(f"{type(e).__name__}: {e}", "工具内部异常，可换用其它工具或稍后重试")
        if executed_args is not None:
            # ── post 钩子：链式改写工具结果（None = 保持原样）────────────
            for hook in self.post_tool_hooks:
                try:
                    rewritten = hook(name, executed_args, result)
                except Exception:
                    log.exception("post_tool_hook 执行失败（忽略）")
                    continue
                if isinstance(rewritten, str) and rewritten:
                    result = rewritten
        if '"error"' in result:
            log.warning("工具 %s 执行出错: %s", name, result)
        return result

    def _record_recent_read(self, name: str, arguments: dict, result: str) -> None:
        """内置 post 钩子：记录 read_file 的成功读取（供压缩后重注入 recent_reads）。

        失败读取不记（读过失败的没有"已被摘要吸收、需要重注入"的意义）；
        列表只留最近 RECENT_READS_KEEP 条，内存占用有界。返回 None = 不改写
        工具结果——本钩子只观察，不干预（见 __init__ 的钩子契约）。
        """
        if name != "read_file":
            return None
        try:
            info = json.loads(result)
            if isinstance(info, dict) and info.get("ok") and info.get("path"):
                self.recent_reads.append(
                    (str(info["path"]), str(info.get("result") or "")[:REINJECT_FILE_CHARS]))
                del self.recent_reads[:-RECENT_READS_KEEP]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return None

    def _log(self, text: str, color: str) -> None:
        log.info(text)  # 同步写进 agent.log（无颜色），终端仍走彩色 print
        if self.verbose:
            print(colored(f"  {text}", color))
