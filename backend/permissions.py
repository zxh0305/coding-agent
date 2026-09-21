"""
最小权限闸门（allow / deny / ask 三态规则引擎）
================================================

把「code_tools 里的静默黑名单拦截」升级为「带原因的权限拒绝」：

  * allow  放行（不产生任何交互，模型无感）；
  * deny   拒绝，原因以工具结果回填给模型（模型据此改道，绝不静默失败）；
  * ask    暂停回合，经 SSE 事件流弹确认卡片请用户决定（见 agent.py / app.py）。

两级规则来源，后者覆盖前者（同 key 即同一 (tool, pattern) 的规则被替换）：
  1. 代码内置默认（本文件 BUILTIN_RULES + 各工具的兜底判定）；
  2. 用户规则：存 settings 表，key = "permissions:<workspace_path>"，JSON 数组
     可手编（后续可挂管理面板）。Agent 不直接 import db——加载器由 app.py 注入
     （本模块保持存储无关，命令行版与单测不引 db 也能跑）。

判定次序：deny > ask > allow（最严者胜）。会话内记住的选择（ask 规则键 →
allow）与本轮一次性决定（overlay）都只作用于 ask 规则，deny 永远不被这两者
翻案——「最严者胜」在记忆之后依然成立。

安全坦白：这是「防误操作 + 强制人工确认」的闸门，不是沙箱。run_bash 拿到的
是真实 shell，工作区只是 cwd 不是牢笼；真正要铁桶就上 Docker
（见 docs/coding-agent-selection.md）。超时拒绝同理——超时不是安全边界，
只是防挂死：无人应答时按拒绝收场，永远站在安全侧。
"""

import fnmatch
import json
import logging
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from code_tools import _resolve

log = logging.getLogger("permissions")

ALLOW = "allow"
DENY = "deny"
ASK = "ask"

# 会话级权限模式（前端输入框下拉三选，存 settings 表按工作区记忆）：
#   confirm（默认）= 内置规则原样生效——只读放行、工作区写入放行、高危命令 ask；
#   readonly       = 只读工具放行，其余（写入/命令）一律 ask——每一步都要确认；
#   yolo           = 全部放行（"完全访问"）——高危规则也只提醒不拦截。
# 模式只影响判定，不影响 deny（用户规则/工作区边界的显式拒绝仍然成立）。
MODES = ("readonly", "confirm", "yolo")

# ask 等待用户决定的默认上限（Web 版）。超时不是安全边界（拒绝才是安全侧），
# 只是防挂死：worker 线程不能为一个再也没人看的卡片等一辈子。
DEFAULT_ASK_TIMEOUT = 300.0

# 用户决策的合法取值（前端确认卡片的三选）
VALID_DECISIONS = ("allow", "allow_session", "deny")


class CommandParseError(ValueError):
    """shlex 无法解析命令（引号不闭合等）。此时不猜测语义，一律交给 ask。"""


# ---------------------------------------------------------------------------
# 命令拆解：run_bash 规则匹配的地基
# ---------------------------------------------------------------------------

# 段边界 token。punctuation_chars=True 时 shlex 会把 ; & | ( ) < 及组合
# （&& || <( 等）切成独立 token，这里是其中"开启新段"的那些：
#   * ; && || | &  —— shell 的命令连接符，之后是新命令；
#   * ( ) <(       —— 子 shell / 进程替换的边界。$(sudo rm -rf /) 这类命令
#     替换在 token 流里正好表现为 "$" "("，靠 "(" 切段即可让替换体内的命令
#     落回"命令词"位置被规则看见。
_SEGMENT_BREAKS = {";", "&", "&&", "|", "||", "(", ")", "<(", ">"}


def split_segments(command: str) -> list[list[str]]:
    """把一条 shell 命令拆成"段"（每个可独立执行的命令一个词序列）。

    为什么必须拆段而不能对原始字符串做 startswith / 子串匹配：
      * startswith("ls") 会被 "ls;rm -rf /" 绕过——整串确实以 ls 开头，
        但分号后面藏着真正的危险命令；只有把分号识别为边界、把 rm 单独
        成段、让它顶到"命令词"位置，规则才看得见它；
      * 反过来，子串/正则全文匹配又会误伤参数："echo 'sudo rm -rf /'"
        里 sudo 只是 echo 的一个引号参数，全文扫描会错杀（旧黑名单正则
        的真实缺陷），测试明确要求这种引号字符串不能命中。
    归一化用 shlex 完成：posix 模式剥引号并保持引号内容为单个 token（所以
    引号里的分号/竖线不会切段、引号里的 sudo 只是一个参数 token），多空白
    合并（"rm  -rf" 与 "rm -rf" 等价），; && || | ( ) 成为独立 token。

    裸换行的特殊处理：shell 把换行当命令分隔符，shlex 却把它当普通空白
    吞掉——不预处理的话 "echo a\\nsudo rm -rf /" 里 sudo 会落到参数位
    置逃过匹配。预处理把换行替换成 ";"，语义完全等价；引号内的换行被
    替换后仍在引号内，shlex 依旧作为单 token 内容保留，不产生假分隔。
    """
    if not command or not command.strip():
        return []
    lexer = shlex.shlex(command.replace("\r", "\n").replace("\n", ";"),
                        posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # 默认会把 # 起注释截断；放宽为不截断，宁可多看不漏看
    try:
        tokens = list(lexer)
    except ValueError as e:  # 引号不闭合等：这条命令 shell 也多半跑不起来，
        raise CommandParseError(f"无法安全解析命令：{e}") from e  # 但绝不猜测，交 ask
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEGMENT_BREAKS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _word_eq(pattern_word: str, token: str) -> bool:
    """一个模式词与一个命令词的匹配。

    三种词形（词边界语义是安全敏感点，逐条说明为什么）：
    1. 短 flag（-xyz）：字符包含——模式里的每个 flag 字符都出现在 token 里。
       这样 "rm -rf" 能命中 "-fr"、"-rF" 的合并写法；而 "rm -r" 单独成规则
       时又能命中 "-r -f" 这种拆开写法（靠两条规则互补覆盖）。
    2. 末尾带 * 的通配词：按【词边界】解释——token 等于星号前缀，或以星号
       前缀开头且紧随其后的字符不是字母/数字。后者用 fnmatch 做基础匹配后
       再加边界守卫。为什么不用纯 fnmatch：fnmatch("pushish", "push*") 为
       真，"git push*" 就会误中 "git pushish"；把星号的管辖范围从"词内字符"
       收缩为"后续参数"，则 "git push origin main" 命中而 "git pushish"
       不命中，同时 "mkfs*" 仍能靠 "." 非字母数字的边界判定罩住 "mkfs.ext4"。
    3. 普通词：逐字符精确相等。
    """
    starred = pattern_word.endswith("*")
    base = pattern_word[:-1] if starred else pattern_word
    if base.startswith("-") and not base.startswith("--") and token.startswith("-"):
        # 短 flag 包含匹配；base 至少要有一个 flag 字符，空串没有意义
        return bool(base[1:]) and all(ch in token for ch in base[1:])
    if starred:
        if token == base:
            return True
        return (token.startswith(base) and len(token) > len(base)
                and not token[len(base)].isalnum())
    return token == pattern_word


def words_match(pattern: str, segment: list[str]) -> bool:
    """命令模式（"git push*" 式的词序列）是否匹配一个命令段。

    语义 = 词序列【前缀】匹配：模式的每个词依次与段的对位词做 _word_eq，
    模式比段长则不可能匹配。末尾的 * 只表示"其后参数任意"，不改变对位。
    """
    words = pattern.split()
    if not words or not segment or len(words) > len(segment):
        return False
    return all(_word_eq(w, tok) for w, tok in zip(words, segment))


# ---------------------------------------------------------------------------
# 规则与判定
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """一条权限规则。pattern 为 None 表示整工具级别；run_bash 的 pattern
    是命令模式词序列。key 即规则身份：用户规则与内置规则同 key 时覆盖，
    会话内记住的选择也以 key 记账。"""

    tool: str
    pattern: str | None
    decision: str
    reason: str = ""

    @property
    def key(self) -> tuple:
        return (self.tool, self.pattern or "")


@dataclass
class Verdict:
    """一次工具调用的最终判定。reason 会出现在确认卡片与拒绝回填里，
    必须是"人能读懂为什么"的话，不能是规则编号。

    ask_keys：本次命中的【全部】ask 规则键（含工具级 ask 的固定键）。一条
    命令往往同时命中多条 ask 规则（如 rm -rf 同时命中 "rm -rf" 与 "rm -r"），
    确认卡只展示第一条的原因，但用户的决定必须同时作用于整组键——否则
    allow_session 的重判会被另一条未被覆盖的 ask 规则再次拦下（真机手测
    抓到过：rm -rf 弹卡允许后，rm -r 规则仍在追问，"本会话不再问"落空）。"""

    verb: str
    reason: str = ""
    rule_key: tuple | None = None
    ask_keys: tuple = ()


# 内置默认规则（第一级来源）。
BUILTIN_RULES = [
    # 只读工具整工具放行：读文件/列目录/搜索/纯函数/看图/天气都不改工作区状态
    Rule("read_file", None, ALLOW, "只读工具"),
    Rule("list_dir", None, ALLOW, "只读工具"),
    Rule("grep", None, ALLOW, "只读工具"),
    Rule("calculator", None, ALLOW, "纯函数计算"),
    Rule("current_time", None, ALLOW, "只读时间查询"),
    Rule("analyze_image", None, ALLOW, "只读图片识别"),
    Rule("get_weather", None, ALLOW, "只读天气查询"),
    # run_bash 高危清单 → ask（粗粒度兜底：宁可多问，用户可用「本会话内允许」
    # 放行同类操作；想彻底放行/禁止可写用户规则覆盖）
    Rule("run_bash", "rm -rf", ASK, "递归强制删除（rm -rf / -fr）"),
    Rule("run_bash", "rm -r", ASK, "递归删除（rm -r，含 -r -f 拆写）"),
    Rule("run_bash", "rm --recursive", ASK, "递归删除（--recursive）"),
    Rule("run_bash", "sudo", ASK, "提权执行（sudo）"),
    Rule("run_bash", "git push*", ASK, "推送远端仓库（git push）"),
    Rule("run_bash", "mkfs*", ASK, "格式化文件系统（mkfs）"),
    Rule("run_bash", "dd", ASK, "底层磁盘读写（dd）"),
    Rule("run_bash", "shutdown", ASK, "关机（shutdown）"),
    Rule("run_bash", "reboot", ASK, "重启（reboot）"),
    Rule("run_bash", "git reset --hard", ASK, "丢弃全部未提交改动（git reset --hard）"),
]

# 工具级兜底（规则清单之外的第三种内置逻辑）：文件写工具的判定不看规则表，
# 直接复用 code_tools._resolve 的工作区边界检查——工作区内 allow、越界 deny。
# 把边界检查从「工具执行时静默抛错」提前到「权限判定时带原因拒绝」，
# 这正是本次改造的题眼。
_WRITE_TOOLS = {"write_file", "apply_patch"}

# 「命令解析失败 → ask」的固定规则键：它不是清单里的规则，但 ask 的
# 等待/恢复、会话记忆必须能对它生效（恢复后重新判定时 overlay 找得到它，
# 否则这条 ask 会永远悬着）。用户若对它选「本会话内允许」，等于明确接受
# 本会话内无法解析的命令直接放行——是他点掉的卡片，语义自洽。
_PARSE_FAIL_KEY = ("run_bash", "<unparseable>")

# 只读模式的固定规则键（BUILTIN_RULES 里 ALLOW 的只读工具集合的镜像）：
# 与 _PARSE_FAIL_KEY 同理，readonly 的逐次确认 ask 也要能被 overlay/
# 会话记忆吸收——用户点过允许之后恢复重判必须找得到这个键。
_READONLY_MODE_KEY_PREFIX = "<readonly_mode>"

READONLY_TOOLS = {"read_file", "list_dir", "grep", "calculator",
                  "current_time", "analyze_image", "get_weather"}


def _builtin_verdict(tool: str, arguments: dict, workspace: Path) -> Verdict:
    if tool in _WRITE_TOOLS:
        try:
            _resolve(str(arguments.get("path") or ""), workspace)
        except ValueError as e:
            # 越界（或路径非法）：带上 _resolve 给出的具体原因拒绝。
            # 不静默：模型必须知道"为什么被拒"才能换工作区内的路径重试。
            return Verdict(DENY, str(e))
        return Verdict(ALLOW, "工作区内写入")
    if tool == "run_bash":
        try:
            split_segments(str(arguments.get("command") or ""))
        except CommandParseError as e:
            # 解析失败不猜测语义：宁可打扰一次，也不让看不懂的命令静默跑掉
            return Verdict(ASK, str(e), _PARSE_FAIL_KEY)
        return Verdict(ALLOW, "常规命令")
    # 已知只读工具已被 BUILTIN_RULES 命中；走到这里的"未知工具"（模型幻觉的
    # 名字）在执行层只会得到"未知工具"错误，没有任何可执行面——放行让执行器
    # 报错即可，fail-closed 在这里没有保护对象。
    return Verdict(ALLOW, "默认放行")


def _parse_user_rules(raw) -> list[Rule]:
    """用户规则 JSON → Rule 列表。非法条目逐条跳过并告警，绝不让一条手编
    JSON 炸掉整个闸门（规则加载失败时退回纯内置，判定照常工作）。"""
    if not isinstance(raw, list):
        return []
    rules = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            log.warning("用户权限规则第 %d 条不是对象，已跳过", i + 1)
            continue
        tool, decision = str(item.get("tool") or ""), str(item.get("decision") or "")
        pattern = item.get("pattern")
        if not tool or decision not in (ALLOW, DENY, ASK):
            log.warning("用户权限规则第 %d 条 tool/decision 非法，已跳过", i + 1)
            continue
        if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
            pattern = None
        if pattern is not None and tool != "run_bash":
            # 命令模式只对 run_bash 有意义；其他工具一律整工具判定（pattern 忽略）
            log.warning("用户权限规则第 %d 条：只有 run_bash 支持 pattern，已按整工具规则处理", i + 1)
            pattern = None
        if pattern is not None and any(
                w.endswith("*") and w[:-1].strip("*") == "" for w in pattern.split()):
            log.warning("用户权限规则第 %d 条：pattern 含空星号词，已跳过", i + 1)
            continue
        rules.append(Rule(tool, pattern, decision, str(item.get("reason") or "")))
    return rules


@dataclass
class _Pending:
    """一次待确认的 ask 请求：规则组粒度（同一组规则一轮只问一次）。
    keys 是本次命中的全部 ask 规则键——决定对整组生效（见 Verdict.ask_keys）。"""
    id: str
    keys: tuple
    tool: str
    arguments: dict
    reason: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None


class PermissionGate:
    """一个会话的权限闸门：规则判定 + ask 的等待/恢复 + 会话内记忆。

    线程模型：check / open_requests / wait_all 全部只在【回合线程】里调用
    （调度分组之前）；resolve 由 HTTP 线程调用（用户点卡片），通过 Event
    唤醒回合线程——等待与判定都绝不落进 ThreadPoolExecutor 的工作线程。
    """

    def __init__(self, workspace: Path, user_rules_loader=None,
                 ask_timeout: float = DEFAULT_ASK_TIMEOUT,
                 mode_loader=None, mode: str = "confirm"):
        # resolve 与 code_tools._resolve 的比较口径对齐：macOS 的 /tmp、
        # /var 等是符号链接，调用方若传入未解析路径，越界判断会把工作区内
        # 的目标误判成区外（smoke test 真实踩过）。resolve 非严格模式，路径
        # 尚不存在也能解析。
        self.workspace = Path(workspace).resolve()
        self._user_rules_loader = user_rules_loader  # fn() -> list[dict]，可缺省
        self.ask_timeout = float(ask_timeout)
        # 权限模式：固定值（CLI/单测）或加载器（Web 版每次判定现读——前端切换
        # 模式下一轮工具调用立即生效，不需要重建会话实例）。非法值一律按 confirm。
        self._mode_loader = mode_loader
        self._mode = mode if mode in MODES else "confirm"
        self._memory: dict[tuple, str] = {}   # 会话内记住：规则 key → ALLOW（会话结束随实例销毁）
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()         # 只保护 _memory/_pending 的读写瞬间

    @property
    def mode(self) -> str:
        if self._mode_loader is not None:
            try:
                m = self._mode_loader()
                return m if m in MODES else "confirm"
            except Exception as e:
                log.warning("权限模式加载失败，退回 confirm：%s", e)
                return "confirm"
        return self._mode

    # ---------- 判定 ----------

    def _effective_rules(self) -> list[Rule]:
        """内置规则 + 用户规则，同 key 用户覆盖内置（保持内置在前、用户在后
        的稳定次序：deny/ask 取"第一个命中者"，次序确定结果才可复现）。"""
        table: dict[tuple, Rule] = {}
        order: list[tuple] = []
        for r in BUILTIN_RULES:
            if r.key not in table:
                order.append(r.key)
            table[r.key] = r
        for r in _parse_user_rules(self._load_user_rules()):
            if r.key not in table:
                order.append(r.key)
            table[r.key] = r  # 覆盖：用户规则顶掉同 key 的内置规则
        return [table[k] for k in order]

    def _load_user_rules(self):
        if self._user_rules_loader is None:
            return []
        try:
            return self._user_rules_loader() or []
        except Exception as e:  # 规则库暂时读不到：降级为纯内置，不阻塞回合
            log.warning("用户权限规则加载失败，本次退回内置规则：%s", e)
            return []

    def check(self, tool: str, arguments: dict, overlay: dict | None = None) -> Verdict:
        """判定一次工具调用。overlay 是本轮一次性决定（规则 key → Verdict），
        由 ask 恢复后重新判定时传入；deny 优先于一切，且不受记忆/overlay 翻案。"""
        mode = self.mode
        with self._lock:
            remembered = dict(self._memory)
        matched: list[tuple[str, str, tuple | None]] = []
        for r in self._effective_rules():
            if not self._rule_hits(r, tool, arguments):
                continue
            verb = r.decision
            if verb == ASK:  # ask 可被「本会话记住」与本轮一次性决定吸收
                if overlay and r.key in overlay:
                    v = overlay[r.key]
                    verb = v.verb
                    matched.append((verb, v.reason, r.key))
                    continue
                if remembered.get(r.key) == ALLOW:
                    matched.append((ALLOW, f"{r.reason}（本会话已允许）", r.key))
                    continue
            matched.append((verb, r.reason, r.key))
        base = _builtin_verdict(tool, arguments, self.workspace)
        if base.verb == ASK:
            # 工具级 ask（解析失败）同样可被本轮决定 / 会话记忆吸收——
            # 不吸收的话，恢复后的重新判定会原样再问一次，回合永远走不出等待
            key = base.rule_key
            if overlay and key in overlay:
                base = overlay[key]
            elif remembered.get(key) == ALLOW:
                base = Verdict(ALLOW, f"{base.reason}（本会话已允许）", key)
        matched.append((base.verb, base.reason, base.rule_key))
        # 最严者胜：deny > ask > allow。同类取第一个命中者（次序见 _effective_rules）。
        for verb in (DENY, ASK):
            for v, reason, key in matched:
                if v == verb:
                    if verb == ASK:
                        if mode == "yolo":
                            # 完全访问：ask 降级为放行（deny 仍按最严者生效，走不到这里）
                            continue
                        # 带上全部命中的 ask 键：确认卡的决定要对整组生效
                        keys = tuple(dict.fromkeys(k for vv, _, k in matched if vv == ASK and k))
                        return Verdict(ASK, reason, key, ask_keys=keys)
                    return Verdict(verb, reason, key)
        if mode == "readonly" and tool not in READONLY_TOOLS:
            # 只读模式：规则放行 ≠ 可直接执行，写入/命令一律先确认。
            # 固定键让这个 ask 能被 overlay/会话记忆吸收（点过允许不重复问）；
            # 键里带工具名，write 与 bash 的确认互不吸收。
            key = (_READONLY_MODE_KEY_PREFIX, tool)
            if overlay and key in overlay:
                v = overlay[key]
                if v.verb == ALLOW:
                    return Verdict(ALLOW, f"{v.reason}", key)
            if remembered.get(key) == ALLOW:
                return Verdict(ALLOW, "只读模式（本会话已允许）", key)
            return Verdict(ASK, "当前为只读模式：写文件与命令执行需要逐次确认",
                           key, ask_keys=(key,))
        return Verdict(ALLOW, "允许", None)

    def _rule_hits(self, rule: Rule, tool: str, arguments: dict) -> bool:
        if rule.tool != tool:
            return False
        if rule.pattern is None:
            return True  # 整工具规则
        if tool != "run_bash":
            return False
        command = str(arguments.get("command") or "")
        try:
            segments = split_segments(command)
        except CommandParseError:
            return False  # 解析失败时模式规则不可信；base 已把整条命令转 ask
        return any(words_match(rule.pattern, seg) for seg in segments)

    # ---------- ask 的等待与恢复 ----------

    def open_requests(self, asks: list[tuple[str, dict, Verdict]]) -> list[dict]:
        """为一批 ask 判定登记待确认请求，返回 SSE 事件载荷列表。

        按【规则键组】去重：请求的任一键已在待确认名单里就不再开卡——
        同一组规则一轮只弹一张卡，用户答一次「本会话内允许」，这组规则
        本轮的全部调用（及后续轮次）一并生效。
        """
        payloads = []
        with self._lock:
            claimed: set[tuple] = {k for p in self._pending.values() for k in p.keys}
            for tool, arguments, verdict in asks:
                keys = verdict.ask_keys or ((verdict.rule_key or (tool, verdict.reason)),)
                if any(k in claimed for k in keys):
                    continue
                pid = uuid.uuid4().hex[:12]
                self._pending[pid] = _Pending(pid, keys, tool, arguments, verdict.reason)
                claimed.update(keys)
                payloads.append({"id": pid, "tool": tool,
                                 "input": arguments, "reason": verdict.reason})
        return payloads

    def resolve(self, request_id: str, decision: str) -> bool:
        """用户决定入口（HTTP 线程）。重复 resolve / 未知 id 返回 False——
        超时后的迟到点击、补发重放出的旧卡片都安全地落在这里。"""
        if decision not in VALID_DECISIONS:
            return False
        with self._lock:
            p = self._pending.get(request_id)
            if p is None or p.decision is not None:
                return False
            p.decision = decision
        p.event.set()
        log.info("权限确认 %s：%s（%s %s）", request_id, decision, p.tool, p.reason)
        return True

    def wait_all(self, cancel: threading.Event | None = None) -> dict[str, str]:
        """阻塞回合线程直到全部待确认有着落。返回 {request_id: decision}。

        只等 0.25s 粒度的分片：cancel（用户点停止）与超时都能及时插进来。
        超时/停止一律按 deny 收场——防挂死与安全侧是同一个方向。
        """
        with self._lock:
            ps = list(self._pending.values())
        deadline = time.monotonic() + max(0.0, self.ask_timeout)
        while True:
            with self._lock:
                undecided = [p for p in ps if not p.event.is_set()]
            if not undecided:
                break
            if cancel is not None and cancel.is_set():
                for p in undecided:
                    p.decision = "cancelled"
                    p.event.set()
                break
            now = time.monotonic()
            if now >= deadline:
                for p in undecided:
                    p.decision = "timeout"
                    p.event.set()
                break
            slot = min(0.25, max(0.0, deadline - now))
            for p in undecided:
                if p.event.wait(slot):
                    break  # 有一个决定到达就回循环重新评估（可能有全部）
        return {p.id: (p.decision or "deny") for p in ps}

    def apply_decisions(self, decisions: dict[str, str]) -> dict[tuple, Verdict]:
        """把用户决定翻译成重判 overlay：规则键 → Verdict。

        决定作用于该请求的【整组】命中键（Verdict.ask_keys 的语义）；其中
        allow_session 顺手把每个键写进会话记忆（本会话内这组规则不再 ask）。
        所有 pending 处理完即销毁，等待队列不会跨轮积压。
        """
        overlay: dict[tuple, Verdict] = {}
        timeout_note = (f"等待确认超时（{int(self.ask_timeout)} 秒），按拒绝处理"
                        if self.ask_timeout > 0 else "当前环境不支持交互确认，按拒绝处理")
        notes = {"deny": "用户选择了拒绝", "timeout": timeout_note,
                 "cancelled": "回合已被停止"}
        with self._lock:
            for pid, d in decisions.items():
                p = self._pending.pop(pid, None)
                if p is None:
                    continue
                if d in ("allow", "allow_session"):
                    if d == "allow_session":
                        for k in p.keys:
                            self._memory[k] = ALLOW
                    for k in p.keys:
                        overlay[k] = Verdict(ALLOW, f"{p.reason}（用户已允许）", k)
                else:
                    reason = f"{p.reason}；{notes.get(d, d)}"
                    for k in p.keys:
                        overlay[k] = Verdict(DENY, reason, k)
        return overlay


def rejection_result(verdict: Verdict) -> str:
    """被拒调用的工具结果回填（deny / ask 被拒共用）。

    不静默失败：模型必须看到原因才能改道（换工作区内路径 / 换掉高危命令），
    hint 则告诉它"被拒不代表任务失败，换方案或请用户调整权限"。
    """
    return json.dumps(
        {"ok": False, "error": f"权限拒绝: {verdict.reason}",
         "hint": "可换用其它方案或请用户调整权限"},
        ensure_ascii=False,
    )
