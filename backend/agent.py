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

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from code_tools import prepare_workspace
from memory import memory_dir, memory_index_block
from permissions import ALLOW, ASK, DENY, PermissionGate, Verdict, rejection_result
from system_prompt import SYSTEM_PROMPT
from tools import TOOL_SCHEMAS, ToolContext, error_result, execute_tool, is_read_only
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
给"之后继续这个任务的助手"看的中文备忘，它会替代原始历史发给模型。必须保留：
1. 任务目标：用户最初要求做什么，后续追加或修改过哪些要求；
2. 已完成的改动清单：创建/修改过哪些文件、执行过哪些关键命令及其结论；
3. 关键文件路径、函数/变量名、重要事实（报错原因、验证是否通过等）；
4. 未完成事项与下一步计划；
5. 用户的重要偏好（沟通语言、代码风格、明确禁止的做法等）。
用简洁的条目式中文输出，不要复述本提示，不要寒暄。细节可以有损，但上述五类信息一条都不能漏。\
"""

# ---------------------------------------------------------------------------
# 工具并行执行：同一轮 tool_calls 里【连续的只读工具】并行跑、写操作串行跑。
# 为什么这样分组是安全的：正确性论证见 Agent._execute_tool_calls；
# 每个工具的 read_only 标记登记在 tools.py 的 TOOL_READ_ONLY。
# ---------------------------------------------------------------------------

PARALLEL_TOOL_WORKERS = 4  # 只读组的最大并发数：读文件/搜索以 IO 等待为主，4 个线程已足够重叠

# 单条工具结果进入历史的长度上限（字符）。这是最后的安全闸：各工具内部虽有
# 各自的输出上限（MAX_READ_LINES / MAX_OUTPUT_CHARS 等），但工具众多、口径
# 不一，且 run_bash `cat 100MB文件` 这类组合仍可能漏出巨型结果。巨型结果一旦
# 落进历史，之后每轮请求都原样重复携带——除了撑爆上下文，还实测触发过供应商
# 内容风控（browser-profiles 里扩展文件的域名表被整读进历史 → 全会话 400，
# 换模型无效，因为污染在 messages 里）。截断保留头部并显式告知余量。
MAX_TOOL_RESULT_CHARS = 60_000

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
                       "②指出未完成的部分和下一步建议。直接输出总结。")

REPEAT_REMIND_TEXT = ("（系统提示：你已连续 {count} 次以完全相同的参数调用工具 {name}。"
                      "不要原样重试——基于已有结果换一个做法：调整参数、换工具、"
                      "说明阻塞在哪里，或直接向用户汇报。）")

BUDGET_REMIND_TEXT = ("（系统提示：本轮已进行到第 {round_no} 轮 / 上限 {max_rounds} 轮。"
                      "请开始收敛：优先完成核心改动，规划好剩余步骤，避免再做大范围探索。）")


class Agent:
    """一个带工具调用能力的对话 Agent。

    参数：
        llm:            提供 chat(messages, tools) -> dict 的客户端（llm_client.py）
        max_rounds:     单次提问内最多"问 LLM"几轮。它只负责兜底：跑满后不再硬砍，
                        而是注入合成指令进入「收尾轮」，让回合以模型自己的真实总结
                        收场（防反复空转另有重复指纹提醒，见 REPEAT_STREAK_REMIND）
        verbose:        是否在终端打印每一轮的思考/工具调用过程（学习时强烈建议开着）
        workspace:      本会话的工作区目录（文件/命令工具的边界）。不传 = 默认工作区
                        （.env 的 WORKSPACE_DIR 或项目 workspace/）。每个任务各自解析，
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
                 artifact_reader=None, permission_gate=None, session_id=None):
        # max_rounds=40：上限只是兜底（真失控另有指纹提醒拦截），合法的长任务
        # （读代码→改→跑验证→再修）经常要几十轮，40 是给它们的余量；到限走
        # 收尾轮（_wrap_up_round）而不是"强制停止"。
        self.llm = llm
        self.system_prompt = system_prompt
        self.max_rounds = max_rounds
        self.verbose = verbose
        self.vision_supported = vision_supported  # 激活模型能否直接看图（决定是否剥离图片输入）
        self.context_window = int(context_window or 0)  # 压缩触发线的基准（providers 表解析链提供）
        self.history: list[dict] = []  # 不含 system 的完整对话历史，跨提问持续累积
        self.trace: list[dict] = []    # 最近一次提问的过程轨迹（轮次/工具调用），供前端展示
        # 增量落盘的指纹账本 {mid: sha1}：save_messages 靠它识别"这条已写过、
        # 内容没变"，每轮只落新增。跨轮随实例存活；进程重启后由恢复的历史重建
        # （db.fingerprints，见 app.py 的会话恢复）。值由 db.save_messages 维护。
        self.saved: dict[str, str] = {}
        # 外置大消息还原器（db.read_artifact）。历史里的归档消息（_artifact 标记，
        # 由 save_messages 落盘时就地替换而来）只有 head/tail 摘要，构造模型视图
        # 时靠它把完整正文读回来。存储注入而非直接 import db：本文件保持存储无关，
        # 命令行版与单测不引 db 也能跑。
        self.artifact_reader = artifact_reader
        # 每字符 token 校准系数（真实 prompt_tokens ÷ 当次请求总字符数）。跨轮缓存：
        # 压缩判断发生在回答结束后，那时没有新 usage，只能靠上一轮校准的系数估算。
        # 压缩后置回 None——摘要的 token 密度与原始日志完全不同，旧系数必然失真，
        # 等下一轮真实 usage 重新校准（见 context_stats / _maybe_compact）。
        self._token_ratio: float | None = None
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

    @staticmethod
    def _clean_outgoing(m: dict) -> dict:
        """发给模型前剥离内部字段（_stats 等下划线前缀），部分服务商会拒绝未知字段。"""
        return {k: v for k, v in m.items() if not k.startswith("_")}

    def _system_content(self) -> str:
        """实际发给模型的 system 内容 = 系统提示词 + 持久记忆索引段。

        提示词正文与记忆契约都在 SYSTEM_PROMPT（system_prompt.py）：契约以
        import 方式拼在其末尾而非复制副本，memory.py 改契约常量两边自动同步。
        这里只补【动态】的索引段（memory_index_block）——索引每次组装从磁盘
        现读，模型/提取线程刚写的记忆下一轮立即可见，读失败由 memory 层降级
        为空，绝不阻塞主循环；契约由此在 system 里恰好出现一次（既不在块里
        重复，也不会漏掉）。

        关键不变式：记忆只进 system 消息，绝不进消息历史——上下文压缩只重写
        消息历史的模型视图（_visible_history）、从不修改 system，因此 compact
        之后记忆原样保留，也不会被重复注入。
        CLI（cli.py）与 Web（app.py）都不传 system_prompt，默认值即
        SYSTEM_PROMPT，注入自动生效；轮末【自动提取】目前只挂 Web worker
        （app.py _run_round 收尾处），CLI 不触发——后续要挂时调
        memory.run_extraction_async 即可，是同一个钩子。
        """
        return self.system_prompt + memory_index_block(memory_dir(self.ctx.workspace))

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
        """估算当前上下文的构成（没有本地分词器，用字符占比反推各部分的 token 份额）。

        有服务商返回的真实 prompt_tokens 时，先用 它/总字符数 校准出每字符 token 系数，
        再按各部分字符数分摊 —— 估算值，但量级和占比是可信的。

        两处与压缩相关的口径：
        1. 字数统计基于【模型视图】而非原始 history——压缩后模型看到的已是摘要，
           按原始历史估算会永远超阈值、反复触发无意义的压缩；
        2. 校准系数缓存在 self._token_ratio（回答结束后的压缩判断靠它），压缩后作废。
        """
        view = self._messages_for_model()
        # system 口径必须与实际请求一致：含记忆段（契约 + 索引），否则记忆
        # 越攒越多时压缩触发线会被系统性低估
        sys_chars = len(self._system_content())
        tool_chars = len(json.dumps(TOOL_SCHEMAS, ensure_ascii=False))
        buckets = {"user": 0, "assistant": 0, "tool": 0}
        for m in view:
            size = len(str(m.get("content") or ""))
            size += len(json.dumps(m.get("tool_calls") or "", ensure_ascii=False))
            if m["role"] in buckets:
                buckets[m["role"]] += size
        total_chars = max(1, sys_chars + tool_chars + sum(buckets.values()))
        if prompt_tokens:
            ratio = prompt_tokens / total_chars
            self._token_ratio = ratio  # 缓存：本轮之后的压缩判断用它估算
        else:
            ratio = self._token_ratio if self._token_ratio else 0.4  # 无实测值时的粗略系数
        est = lambda chars: round(chars * ratio)
        return {
            "system": est(sys_chars),
            "tools": est(tool_chars),
            "user": est(buckets["user"]),
            "assistant": est(buckets["assistant"]),
            "tool_results": est(buckets["tool"]),
        }

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
        self.history.append(user_message or {"role": "user", "content": user_input})
        # 提取本轮附带的图片（OpenAI content 数组里的 image_url 部分），挂进工具上下文。
        # 主模型看不见像素；analyze_image 工具借"视觉模型"看图时用的就是这份数据。
        content = (user_message or {}).get("content")
        if isinstance(content, list):
            self.ctx.images = [p for p in content if p.get("type") == "image_url"]
        else:
            self.ctx.images = []
        # 回合内提醒状态（防失控，见 _maybe_remind）：重复指纹 streak、提醒预算、
        # 预算提醒轮数（上限前 10 轮、前 4 轮各提醒一次）。每回合重置。
        self._streak_sig = None
        self._streak_count = 0
        self._reminders_used = 0
        self._budget_remind_rounds = {self.max_rounds - 10, self.max_rounds - 4}
        start = time.time()
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # 跨轮回调状态（主循环与收尾轮共用一套）：缓存命中率、上下文估算、计时起点。
        # 由 _consume_stream / 两个收尾方法就地更新。
        metrics = {"start": start, "cache_hit_rate": None,
                   "context": self.context_stats()}  # 还没发过请求时给个纯估算

        for round_no in range(1, self.max_rounds + 1):
            if self.cancel_event.is_set():
                # 工具结果刚入完历史就被叫停：历史以上一条 tool 消息结尾，依然合法
                break
            self._log(f"── 第 {round_no} 轮：请求 LLM ──", "gray")
            self.trace.append({"type": "round", "round": round_no})
            yield "round", {"round": round_no}

            # 每轮都重发【系统提示 + 完整历史】—— 这就是 LLM 的全部"记忆"
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
            assistant_msg = yield from self._consume_stream(messages, TOOL_SCHEMAS,
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
            yield from self._execute_tool_calls(tool_calls)

            # 防失控提醒：不是砍停——检测到死循环苗头 / 轮数接近上限时注入合成
            # user 提醒，让模型下一轮自己纠偏（触发规则与预算见 _maybe_remind；
            # 提醒静默进历史，不发任何事件）。
            self._maybe_remind(tool_calls, round_no)

        # 走到循环外只有两种情况：被用户停止，或跑满 max_rounds
        if self.cancel_event.is_set():
            log.info("生成被用户停止（未在流式阶段截住）")
            yield "done", {"answer": "（已手动停止）",
                           "elapsed_s": round(time.time() - metrics["start"], 1),
                           "usage": usage_total, "cache_hit_rate": metrics["cache_hit_rate"],
                           "context": metrics["context"], "stopped": True}
            return
        # 跑满 max_rounds：轮数上限的新语义是「触发收尾」而非「强制杀死」——
        # 注入合成指令，以 tools=None 请求一轮真实总结，回合以模型自己的总结
        # + done 收场（收尾轮与普通回答同一套完成后压缩判断，见 _wrap_up_round）。
        yield from self._wrap_up_round(usage_total, metrics)

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
                yield "reasoning_delta", {"delta": payload}
            elif kind == "usage":  # 本轮 token 用量 → 累计后实时推给前端
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage_total[key] += payload.get(key) or 0
                hit = payload.get("prompt_cache_hit_tokens")
                miss = payload.get("prompt_cache_miss_tokens")
                if hit is not None and (hit + (miss or 0)) > 0:
                    metrics["cache_hit_rate"] = round(hit / (hit + miss) * 100, 1)
                metrics["context"] = self.context_stats(prompt_tokens=payload.get("prompt_tokens"))
                yield "usage", {**usage_total, "elapsed_s": round(time.time() - metrics["start"], 1),
                                "cache_hit_rate": metrics["cache_hit_rate"],
                                "context": metrics["context"]}
            else:
                assistant_msg = payload
        # 本轮推理文本收尾后整段入 trace（放流结束而非每个 delta 追加：一条条目
        # 一个轮次，回放时渲染成一个思考块）。空串不入，避免无思考模型多出空块。
        text = "".join(reasoning_parts)
        if text:
            self.trace.append({"type": "reasoning", "round": round_no, "text": text})
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
                                        "stopped": True}})
        log.info("耗时 %.1fs · 用户中途停止", elapsed)
        yield "done", {"answer": answer, "elapsed_s": elapsed, "usage": usage_total,
                       "cache_hit_rate": metrics["cache_hit_rate"],
                       "context": metrics["context"], "stopped": True}

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
                                        "cache_hit_rate": metrics["cache_hit_rate"]}})
        log.info("耗时 %.1fs · tokens 输入 %d / 输出 %d",
                 elapsed, usage_total["prompt_tokens"], usage_total["completion_tokens"])
        payload = {"answer": answer, "elapsed_s": elapsed, "usage": usage_total,
                   "cache_hit_rate": metrics["cache_hit_rate"], "context": metrics["context"]}
        if stopped_reason:
            payload["stopped_reason"] = stopped_reason
        yield "done", payload

    def _wrap_up_round(self, usage_total: dict, metrics: dict):
        """收尾轮：跑满 max_rounds 后注入合成 user 指令，以 tools=None 请求一轮
        真实总结，让回合以模型自己的总结收场（替换旧的"强制停止"兜底文案）。

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
        round_no = self.max_rounds + 1
        self.history.append({"role": "user", "_synthetic": True,
                             "content": WRAP_UP_INSTRUCTION.format(max_rounds=self.max_rounds)})
        log.warning("达到最大轮数 %d，进入收尾轮（禁工具总结）", self.max_rounds)
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

    def _inject_reminder(self, text: str, kind: str, detail: dict) -> bool:
        """往历史里注入一条合成 user 提醒。受 MAX_TURN_REMINDERS 总预算约束，
        预算耗尽后一律放弃（提醒是提示性的，预算保证了它永远无法反过来绑架
        回合）。返回是否真正注入。"""
        if self._reminders_used >= MAX_TURN_REMINDERS:
            return False
        self._reminders_used += 1
        self.history.append({"role": "user", "_synthetic": True, "content": text})
        self.trace.append({"type": "system_reminder", "kind": kind, **detail})
        log.info("注入合成提醒（%s），本回合已用 %d/%d",
                 kind, self._reminders_used, MAX_TURN_REMINDERS)
        return True

    def _maybe_remind(self, tool_calls: list[dict], round_no: int) -> None:
        """工具结果入历史后检查两类提醒（静默进历史，不发任何事件——下一轮
        请求模型自然看到）：

        1. 循环提醒：按【请求顺序】逐个更新重复指纹 streak（与 _execute_tool_calls
           的回填顺序一致，论证见其 docstring「回填顺序只认请求顺序」）。同一
           签名连续达到 REPEAT_STREAK_REMIND 次才提醒，且同一段连续重复内只提醒
           一次（== 阈值才触发，第 4、5 次不再触发）；签名变化即重置计数。
        2. 轮数预算提醒：round_no 进入预算提醒轮数集合（max_rounds-10 / -4 各
           一次）时提醒模型收敛。

        两类提醒共享 _reminders_used 预算（见 _inject_reminder）。
        """
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            sig = self._tool_signature(name, fn.get("arguments"))
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
        本方法只改内存里的 self.history；DB 原文不受影响（与压缩同一口径：
        存储永远完整，只有模型视图被瘦身）。
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
            saved += len(content)
            plan.append((i, content))

        if saved < CLEAR_TOOL_RESULTS_MIN_SAVING:
            return 0  # 省的还不够塞牙缝，不值得动（保护原始内容）

        cleared = 0
        for i, _orig in plan:
            m = self.history[i]
            m["content"] = CLEARED_TOOL_RESULT_PLACEHOLDER
            m["_tool_result_cleared"] = True  # 标记（下划线前缀，发给模型前会被剥离）
            cleared += 1

        if cleared:
            # 清理改变了字符总量 → 校准系数作废（与压缩同一理由：系数是按
            # 原始字符量校准的，内容换了密度就变了）。下轮真实 usage 重新校准。
            self._token_ratio = None
            log.info("分级压缩①：清理 %d 条较早工具结果（省约 %d 字符，保留最近 %d 条）",
                     cleared, saved, keep_recent)
        return cleared

    def _maybe_compact(self) -> dict | None:
        """回答结束后调用：估算超阈值就把中段历史压缩成一条边界标记。

        分级：超阈值先试**无损**的"清旧工具结果"（_clear_old_tool_results），
        清完重新估算；仍超阈值才做**有损**的摘要。这样能省下不少"本可不必
        摘要"的场景——工具输出往往是上下文大头，清掉它经常就够了。

        返回给前端的事件载荷（未触发/失败返回 None）。任何异常都不往外抛——
        压缩是"锦上添花"，绝不能让它打断会话；失败就跳过，下一轮回答结束后
        阈值依然超着，自然会重试。
        """
        if not self.context_window:
            return None
        stats = self.context_stats()  # 用上一轮真实 usage 校准过的系数估算（无则粗略 0.4）
        est_before = sum(stats.values())
        if est_before <= self.context_window * COMPACT_THRESHOLD:
            return None

        # ── 分级压缩第一档：先清较早的工具结果（无损、不花 LLM 调用）──
        # 清完重新估算；降到阈值以下就直接收工，不必动摘要——省下一次有损
        # 总结，也省一次 LLM 调用。前端不产卡片（没有语义损失，无需告知）。
        if self._clear_old_tool_results():
            stats = self.context_stats()
            est_after_clear = sum(stats.values())
            if est_after_clear <= self.context_window * COMPACT_THRESHOLD:
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
            return None
        # 视图结构：[首条用户消息] + [旧摘要(若有)] + [活区消息]。
        # head = 活区在视图里的起始下标（第一条"可压缩"消息）；无边界时视图就是
        # 原始 history 本身，活区从首条用户消息之后开始。
        has_boundary = last_boundary >= 0
        head = 2 if has_boundary else first_user + 1
        cut = self._compact_split(view, head)
        if cut is None:
            log.warning("上下文估算 %d tokens 超过窗口 %d 的 %.0f%%，但没有可安全压缩的段落，跳过",
                        est_before, self.context_window, COMPACT_THRESHOLD * 100)
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
        try:
            # 复用会话同一个 LLM 与停止开关（chat_stream 带看护线程，用户等不及点
            # 停止也能掐断这次总结）；delta 片段直接丢弃——压缩不产生回答流。
            for kind, payload in self.llm.chat_stream(messages=request, cancel=self.cancel_event):
                if kind == "message":
                    reply = payload
        except Exception as e:  # 网络/服务商错误：跳过本轮，下轮重试，绝不打断会话
            log.warning("上下文压缩失败（%s），将在下一轮回答结束后重试", e)
            return None
        summary = ((reply or {}).get("content") or "").strip()
        if not summary or self.cancel_event.is_set():
            return None  # 被停止掐断的半截总结不可信，作废重来

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
        # 校准系数作废：它是在"原始历史"的字符总量上校准的，压缩后请求里换成
        # 了摘要（token 密度完全不同），旧系数会把估算带偏；置回 None 让
        # context_stats 退回粗略系数，等下一轮真实 usage 到达再重新校准。
        self._token_ratio = None

        stats_after = self.context_stats()
        log.info("上下文压缩完成：估算 %d → %d tokens（保留最近 %d 条，摘要 %d 字）",
                 est_before, sum(stats_after.values()), len(view) - cut, len(summary))
        return {"summary": summary, "prompt_tokens": sum(stats_after.values()), "context": stats_after}

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
           （read_file / list_dir / grep 只打开文件读，calculator / current_time
           是纯函数），彼此没有数据依赖——谁先谁后执行，各自的输出都一样。
           并行只是把总耗时从"各调用相加"变成"取最慢者"；
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
                # create_doc 成功：额外产出一条 doc_created 事件（走 app.py 的
                # else 分支进 SSE 总线），前端据此自动弹出右侧文档面板。
                # 失败（result 含 error）不推——没生成成功没什么可弹的。
                name = (call.get("function") or {}).get("name", "")
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

    def _backfill_tool_result(self, call: dict, result: str):
        """把一个工具结果按请求位置回填：追加历史、记轨迹、产出事件。
        正常执行与权限拒绝共用同一条回填路径——对下游（历史配对/前端渲染）
        而言，拒绝结果就是一个普通的（带 error 的）工具结果。"""
        if len(result) > MAX_TOOL_RESULT_CHARS:
            keep = MAX_TOOL_RESULT_CHARS
            result = (result[:keep]
                      + f"\n…[工具结果过长（共 {len(result)} 字符），已截断至前 {keep} 字符。"
                        "需要余下内容请用更窄的参数分段读取（offset/limit、grep、head 等），"
                        "不要重试同样的大范围读取]")
            self._log(f"✂️ 工具结果超长，已截断至 {keep} 字符", "yellow")
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
        """
        name = (call.get("function") or {}).get("name", "")
        raw_args = (call.get("function") or {}).get("arguments") or "{}"
        try:
            # 注意坑点：arguments 是【JSON 字符串】不是 dict（模型输出的是文本）
            arguments = json.loads(raw_args)
            if not isinstance(arguments, dict):
                arguments = {}
        except json.JSONDecodeError:
            arguments = {}
        try:
            result = execute_tool(name, arguments, self.ctx)
        except Exception as e:  # execute_tool 已兜底一次；这里再兜一层，守住"绝不抛"的承诺
            result = error_result(f"{type(e).__name__}: {e}", "工具内部异常，可换用其它工具或稍后重试")
        if '"error"' in result:
            log.warning("工具 %s 执行出错: %s", name, result)
        return result

    def _log(self, text: str, color: str) -> None:
        log.info(text)  # 同步写进 agent.log（无颜色），终端仍走彩色 print
        if self.verbose:
            print(colored(f"  {text}", color))
