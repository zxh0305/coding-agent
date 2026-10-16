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
是真实 shell，工作区只是 cwd 不是牢笼；restricted 模式的白名单（含工作区
路径参数校验）把常规命令的静态审查做严，但静态审查永远有保守余量——含
$/反引号的命令一律转 ask 就是这个原因；真正要铁桶就上 Docker
（docs/coding-agent-selection.md，executor.py 的 DockerExecutor seam）。
超时拒绝同理——超时不是安全边界，只是防挂死：无人应答时按拒绝收场，
永远站在安全侧。
"""

import fnmatch
import json
import logging
import re
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

# 会话级权限模式（前端输入框下拉四选，存 settings 表按工作区记忆）：
#   readonly       = 只读工具放行，其余（写入/命令）一律 ask——每一步都要确认；
#   restricted     = 受限档：run_bash 走【白名单】（名单内放行、名单外一律 ask，
#                    见 BASH_ALLOWLIST），其余工具与 confirm 相同；
#   confirm（默认）= 内置规则原样生效——只读放行、工作区写入放行、高危命令 ask；
#   yolo           = 全部放行（"完全访问"）——高危规则也只提醒不拦截。
# 模式只影响判定，不影响 deny（用户规则/工作区边界的显式拒绝仍然成立）。
# 严格度：readonly > restricted > confirm > yolo（左严右松）。
MODES = ("readonly", "restricted", "confirm", "yolo")

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


# ---------------------------------------------------------------------------
# 透明前缀剥离：让真命令回到"命令词"位置
# ---------------------------------------------------------------------------
#
# 为什么必须做：规则匹配是【位置 0 前缀匹配】(words_match)，而 shell 里有一批
# "包装器"只负责改变执行环境、不改变真正要跑的命令——`env rm -rf /tmp/x` 与
# `rm -rf /tmp/x` 语义完全相同，但前者的段是 ['env','rm','-rf','/tmp/x']，
# rm 掉到位置 1，`rm -rf` 规则看不见它。实测以下全部 allow（默认 confirm 模式）：
#     env rm -rf /tmp/x      nice sudo ls         xargs rm -rf
#     bash -c "rm -rf /tmp"  HELLO=1 sudo ls      command rm -rf /tmp/x
# 这是安全叙事上最糟的一类洞：README 承诺"rm -rf / sudo / git push 等先弹确认"，
# 而加一个 `env ` 就绕过去了。剥离后再匹配，规则回到"看真命令"的语义上。
#
# 保守原则：只剥"确定不改变后续命令语义"的包装，剥不掉的原样保留（宁可漏剥
# 让规则不命中、也不要错剥导致误伤）。`sh -c <串>` 是例外——它后面跟的是一整
# 段 shell 源码（单个 token），必须【再拆一次】才能看见里面的 rm；这一步是
# 递归的（`env sh -c "bash -c 'rm -rf /'"` 要剥两层）。
#
# 【命令词归一】剥离之外还有一件事：把位置 0 的**绝对路径**归一成 basename。
# 规则是词精确匹配，`/bin/rm` != `rm`，于是 /bin/、/usr/bin/ 前缀曾让【全部】
# 高危规则失效（rm -rf / sudo / git push / chmod / dd / mkfs / truncate…）。
# 绝对路径恰恰是最常见的写法（sh 的 hash 表、脚本、包管理器都用）。实测 200 个
# 「4 种路径前缀 × 5 种包装器 × 10 条高危命令」组合，修前 5 类全部可绕、修后 0 条。
# 只归位置 0；参数位置的路径保持原样（`cat /etc/rm.conf` 不该命中 rm 规则）。
_WRAPPERS = {"env", "nice", "nohup", "command", "time", "stdbuf", "setsid", "ionice",
             # 补漏（实测可绕过全部高危规则）：
             #   timeout 5 rm -rf /x   → timeout 吃一个"时长"参数后才是真命令
             #   eval rm -rf /x        → eval 把余下当命令再执行一次
             #   builtin rm -rf /x     → builtin 强制走内建（对 rm 无意义但语法合法）
             #   doas / pkexec         → 提权包装，与 sudo 同类
             # exec 会把当前进程替换成目标命令，语义上等于直接执行
             "timeout", "eval", "builtin", "doas", "pkexec", "exec"}
# 带 -c 语义的 shell 包装：其后的 token 是被执行的源码，需要递归再拆
_SHELL_C = {"sh", "bash", "zsh", "dash", "ksh"}
# 包装器自身可能带的、需要额外吃一个参数的选项（如 nice -n 10 cmd）
_WRAPPER_ARG_OPTS = {"-n", "--adjustment", "-u", "--user", "--nice"}
# 需要额外吃一个【位置参数】的包装器：timeout 的时长是【必需】的，
# 少了这张表 `timeout 5 rm -rf /x` 剥完 timeout 后会把 "5" 当成命令词，
# 于是 rm 掉到位置 1、规则照样看不见它（补了包装器名却仍绕过）。
#
# 【注意 doas/pkexec 不在此列】它们的用户名是可选的：`doas ls` 与
# `doas root ls` 都合法。若按"必需位置参数"处理，`doas rm -rf /x` 会把
# 真命令 rm 当作用户名吃掉，反而放行（实测第一版就是这样漏了 20 条）。
# 对它们只吃【纯选项】，不猜位置参数——宁可少剥一层，让规则在 `doas root rm`
# 上漏判（退化成 ask 兜底），也不要把命令吃掉。
_WRAPPER_POSITIONAL = {"timeout"}

# 内联脚本里的「破坏性调用」特征（见 _semantic_asks 的 d 分支）。
# 只匹配会删/改/覆盖文件或起进程的调用，不匹配普通 import/print——否则
# `python3 -c "import demo; print(demo.add(2,2))"` 这种最常用的验证手段
# 每次都要弹卡，用户最终会关掉确认模式，得不偿失。
_DESTRUCTIVE_SCRIPT = re.compile(
    r"shutil\.rmtree|os\.remove|os\.unlink|os\.rmdir|os\.removedirs"
    r"|os\.system|os\.popen|subprocess\.|os\.chmod|os\.chown|shutil\.move"
    r"|open\([^)]*['\"][wa]|Path\([^)]*\)\.unlink|\.write_text|\.write_bytes"
    r"|sys\.exit|exec\(|eval\(")


def _is_shell_c_flag(tok: str) -> bool:
    """该 token 是否表示 shell 的 `-c`（把余下参数当源码执行）。

    不能只判 `tok == "-c"`：`bash -lc "rm -rf /"` 里 `-lc` 是**单个 token**
    （-l 与 -c 合并），旧判定认不出，于是"递归拆 shell 源码"这条防线在组合
    短选项下直接失效。实测修前 `/bin/bash -lc "rm -rf /tmp/x"` 会走到规则
    之外（见下方实测表）。

    规则：以 - 开头、不是长选项（--），且字符集里含 c。`-c`、`-lc`、`-xc`
    都命中；`--login`、`-l` 不命中。
    """
    if not tok.startswith("-") or tok.startswith("--"):
        return False
    return "c" in tok[1:]


def _unwrap_segment(seg: list[str], _depth: int = 0) -> list[list[str]]:
    """把一个命令段剥掉透明前缀，返回它真正代表的命令段列表。

    返回列表是因为 `sh -c "a; b"` 展开后可能是多段。剥不动时返回 [seg] 原样。
    """
    if not seg or _depth > 4:          # 深度上限：防构造的嵌套把解析拖爆
        return [seg]
    out: list[list[str]] = []
    rest = list(seg)
    while rest:
        head = rest[0]
        # 【命令词归一】取 basename 再判包装器与后续规则。
        # 为什么必须做：shell 里 /bin/rm、/usr/bin/sudo 与裸 rm、sudo 完全等价，
        # 但规则是【词精确匹配】，少一个归一就让 /bin/ 前缀绕过全部高危规则。
        # 实测（修前）：/bin/rm -rf /x、/usr/bin/sudo …、/bin/dd … 全部 allow。
        # 只对【位置 0】归一——那个位置才是命令词；参数位置的 /bin/rm 是普通
        # 字符串（`cat /etc/rm.conf` 不该命中 rm 规则），保持原样。
        head_base = head.rsplit("/", 1)[-1] if "/" in head else head
        # 1) VAR=value 形式的临时环境变量赋值：只影响环境，不改变命令
        if "=" in head and not head.startswith("-") and "/" not in head.split("=", 1)[0]:
            rest = rest[1:]
            continue
        # 2) 包装器（env / nice / timeout / eval / …）：吃掉自己与它的选项
        if head_base in _WRAPPERS:
            rest = rest[1:]
            while rest and rest[0].startswith("-"):
                opt = rest[0]
                rest = rest[1:]
                if opt in _WRAPPER_ARG_OPTS and rest:
                    rest = rest[1:]        # 该选项还带一个值
            # 2a) 位置参数（timeout 的时长、doas/pkexec 的用户名）：再吃一个
            #     非选项词，否则它会被当成命令词、真命令又被挤到位置 1。
            if head_base in _WRAPPER_POSITIONAL and rest and not rest[0].startswith("-"):
                rest = rest[1:]
            continue
        # 2b) xargs：它把余下的词当【要执行的命令】。`xargs rm -rf` 里 rm 在位置
        #     1，剥离后回到位置 0 被规则看见。xargs 自己的选项（-0 -n1 -I{}）
        #     先吃掉；`-I{}` 还带一个值。
        if head_base == "xargs":
            rest = rest[1:]
            while rest and rest[0].startswith("-"):
                opt = rest[0]
                rest = rest[1:]
                if opt in ("-I", "-n", "--max-args", "-P", "--max-procs",
                           "-s", "--max-chars") and rest:
                    rest = rest[1:]
            continue
        # 3) shell -c <源码>：把源码当命令再拆一遍（递归，可多层）
        #    `-c` 可能与其它短选项合并成单 token（`bash -lc "rm -rf /"`），
        #    故按"短选项串里含 c"识别，而不是只认恰好等于 "-c"。
        if head_base in _SHELL_C and len(rest) >= 2 and _is_shell_c_flag(rest[1]):
            src = " ".join(rest[2:])
            try:
                inner = split_segments(src)
            except CommandParseError:
                return [seg]               # 拆不动就退回原段（规则不命中即 ask 兜底）
            for sub in (inner or [rest]):
                out.extend(_unwrap_segment(sub, _depth + 1))
            return out or [seg]
        # 4) 已经不是包装器了：剩下的就是真命令。
        #    【必须先归一命令词再交给规则】——规则是词精确匹配，若这里原样留着
        #    `/bin/rm`，"rm -rf" 规则就永远命不中它（前面只把 base 用于判包装器，
        #    不改写 rest 的话，绝对路径绕过依旧成立——实测修版第一版就是这样）。
        #    只改位置 0；参数保持原样（`cat /etc/rm.conf` 不该命中 rm 规则）。
        if head_base != head:
            rest = [head_base, *rest[1:]]
        out.append(rest)
        return out
    return out or [seg]


def normalize_segments(command: str) -> list[list[str]]:
    """split_segments + 透明前缀剥离。规则匹配一律用它，不用裸 split_segments——
    否则 `env rm -rf /` 这类写法直接绕过全部 ask 规则（见 _unwrap_segment 注释）。"""
    normalized: list[list[str]] = []
    for seg in split_segments(command):
        normalized.extend(_unwrap_segment(seg))
    return normalized


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
    # 只读工具整工具放行：读文件/列目录/搜索/看图都不改工作区状态
    Rule("read_file", None, ALLOW, "只读工具"),
    Rule("list_dir", None, ALLOW, "只读工具"),
    Rule("grep", None, ALLOW, "只读工具"),
    Rule("analyze_image", None, ALLOW, "只读图片识别"),
    Rule("todo_write", None, ALLOW, "任务清单（仅会话内存）"),
    Rule("spawn_subagent", None, ALLOW, "只读子代理（子代理工具面无写权限，"
                       "结论回传主代理）"),
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
    Rule("run_bash", "git clean", ASK, "清理未跟踪文件（git clean，-fdx 会删掉整片目录）"),
    Rule("run_bash", "chmod", ASK, "修改文件权限（chmod，-R 作用于整棵树）"),
    Rule("run_bash", "chown", ASK, "修改文件属主（chown）"),
    Rule("run_bash", "truncate", ASK, "截断文件（truncate，会把文件清零）"),
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

READONLY_TOOLS = {"read_file", "list_dir", "grep", "analyze_image", "todo_write",
                  # create_doc 并非真的只读，但文档生成视为【低危】：无论哪种权限
                  # 模式都直接放行、不弹确认卡。放进本集合即达成——只读模式下的
                  # "非本集合工具一律 ask"（见 check）不再对它生效。
                  "create_doc"}

# 浏览器工具整工具 ask（confirm 模式）：真实浏览器带本地登录态出网、可点击/
# 填表，操作面大于工作区工具——每个动作先弹确认卡，用户可用「本会话内允许」
# 一次性放行。readonly 模式下它们同样不在 READONLY_TOOLS，逐次 ask，天然覆盖。
BROWSER_ASK_RULES = [
    Rule("browser_open", None, ASK, "打开网页（内置浏览器，复用本地登录态）"),
    Rule("browser_click", None, ASK, "点击页面元素（内置浏览器）"),
    Rule("browser_type", None, ASK, "向网页输入内容（内置浏览器）"),
    Rule("browser_screenshot", None, ASK, "页面截图（内置浏览器）"),
]


# ---------------------------------------------------------------------------
# restricted 模式的 run_bash 白名单（受限档：名单内放行、名单外一律 ask）
# ---------------------------------------------------------------------------

# 语义与上面的内置 ask 清单相反：ask 清单是黑名单（列出的才拦，其余放行），
# 白名单是"只有列出的才放行"。匹配语义随之反转：黑名单 any(命中即拦)，
# 白名单 all(每一段命中才放)。初版名单宁紧勿松，只收无副作用面 + git 只读
# 子命令（逐条列出、不收裸 "git branch"/"git remote"——它们的前缀匹配会把
# "git branch -D x"/"git remote add" 这类写操作一起放进）。想放行名单外的
# 命令用用户规则点名（如 {"tool":"run_bash","pattern":"curl","decision":"allow"}），
# 用户 allow 规则命中时白名单判定整体跳过——用户显式意志 > 模式默认。
BASH_ALLOWLIST = (
    # 文件/目录查看与无害输出
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "tree", "which", "echo",
    # 搜索
    "grep", "find",
    # git 只读子命令
    "git status", "git log", "git diff", "git show", "git blame", "git rev-parse",
)

# 这些命令的【非 flag 参数】按工作区内路径校验（resolve 后必须仍位于工作区）：
# cat/grep 等能读任意绝对路径，cwd 在工作区并不构成边界；把参数锁进工作区
# 才配得上"受限"二字。相对路径（含 *.py 这类 glob 字样）按字面解析进工作区，
# 天然满足；绝对路径与 .. 逃逸一律拦下转 ask。
_WS_PATH_ARG_COMMANDS = {"ls", "cat", "head", "tail", "wc", "file", "tree",
                         "grep", "find", "echo"}

# find 的动作族 flag：-exec/-execdir/-ok/-okdir 可以执行任意命令，
# -delete/-fprint*/-fls 是写操作——命中即整段不通过（find 的搜索能力保留）。
_FIND_ACTION_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls"}

# 受限模式的两个固定 ask 键（与 _PARSE_FAIL_KEY 同理：必须能被卡片决定/
# 会话记忆吸收，否则用户点过「本会话内允许」后重判仍会再问一遍）。
_NOT_ALLOWLISTED_KEY = ("run_bash", "<not-allowlisted>")
_EXPANSION_KEY = ("run_bash", "<shell-expansion>")


def _path_args_in_workspace(args: list[str], workspace: Path) -> bool:
    """白名单命令的非 flag 参数是否全部落在工作区内（_allowlist_hit 的守卫）。

    workspace/tok：tok 是绝对路径时 pathlib 拼接会【整个替换掉】基准目录——
    正好完成"绝对路径必越界"的判定；相对路径（含 glob 字样）按字面解析进
    工作区内，天然满足。resolve 非严格模式，不存在的路径也能解析（与
    PermissionGate.__init__ 对 workspace 的处理同口径）。
    """
    for tok in args:
        if tok.startswith("-"):
            continue  # flag（-la / -name / --pretty=...）不按路径校验
        try:
            target = (workspace / tok).resolve()
        except (OSError, ValueError):
            return False
        if target != workspace and workspace not in target.parents:
            return False
    return True


def _allowlist_hit(segment: list[str], workspace: Path,
                   extra_patterns: tuple = ()) -> bool:
    """一个命令段是否命中白名单：词序列前缀匹配（复用 words_match 的词边界
    语义）+ 每命令的附加守卫。首个命中的模式即定结果——同一 head 不会命中
    第二个模式，首匹配无歧义。extra_patterns 是用户 allow 规则点名的附加
    命令模式（它们不走工作区路径守卫——用户显式意志优先）。"""
    if not segment:
        return False
    for pattern in (*BASH_ALLOWLIST, *extra_patterns):
        words = pattern.split()
        if len(words) > len(segment):
            continue
        if not all(_word_eq(w, tok) for w, tok in zip(words, segment)):
            continue
        if words[0] == "find" and any(
                tok in _FIND_ACTION_FLAGS or tok.startswith("-fprint")
                for tok in segment):
            return False
        if words[0] in _WS_PATH_ARG_COMMANDS:
            return _path_args_in_workspace(segment[len(words):], workspace)
        return True
    return False


def _builtin_verdict(tool: str, arguments: dict, workspace: Path,
                     bridge: dict | None = None) -> Verdict:
    if tool in _WRITE_TOOLS:
        try:
            # bridge：沙箱桥接根（attachments/、.agent-memory/ → 数据区），与
            # 执行层 code_tools._resolve 同一份表、同一套校验——权限层不会比
            # 执行层更严或更松。
            _resolve(str(arguments.get("path") or ""), workspace, bridge)
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
                 mode_loader=None, mode: str = "confirm",
                 bridge_roots_loader=None):
        # bridge_roots_loader：fn() -> {前缀: 绝对路径}，与 Agent.ctx.bridge_roots
        # 同源（session_boot 构造闸门时传入）。None（CLI/单测）= 无桥接。
        self.bridge_roots_loader = bridge_roots_loader
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

    @property
    def user_rules_loader(self):
        """用户规则加载器的只读暴露。子代理构造自己的闸门（ask_timeout=0）
        时必须复用同一加载器：用户的自定义 deny 规则若不跟过去，子代理就成了
        绕过个性化禁令的旁路（父代理被禁读的路径子代理能读）；allow 规则同理
        生效。ask 规则带下去无害——子代理 ask_timeout=0，ask 立即按拒绝收场。"""
        return self._user_rules_loader

    @property
    def pending_count(self) -> int:
        """当前有多少 ask 请求正等用户确认（0 = 没卡在闸门上）。

        供任务列表算"等待确认"状态用：回合线程此刻阻塞在 wait_all 上，
        从 HTTP 线程读这个数必须是安全的——只取一次 _lock 快照。
        """
        with self._lock:
            return len(self._pending)

    # ---------- 判定 ----------

    def _effective_rules(self) -> list[Rule]:
        """内置规则 + 用户规则，同 key 用户覆盖内置（保持内置在前、用户在后
        的稳定次序：deny/ask 取"第一个命中者"，次序确定结果才可复现）。"""
        table: dict[tuple, Rule] = {}
        order: list[tuple] = []
        for r in [*BUILTIN_RULES, *BROWSER_ASK_RULES]:
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

    @staticmethod
    def _absorb(overlay: dict | None, remembered: dict, verb: str, reason: str,
                key: tuple) -> list[tuple[str, str, tuple]]:
        """把一条 ask 判定过一遍「本轮一次性决定 → 会话记忆」，返回吸收后的
        单元素列表（前缀规则与语义规则共用这一份吸收语义，避免两处漂移）。"""
        if overlay and key in overlay:
            v = overlay[key]
            return [(v.verb, v.reason, key)]
        if remembered.get(key) == ALLOW:
            return [(ALLOW, f"{reason}（本会话已允许）", key)]
        return [(verb, reason, key)]

    def _semantic_asks(self, tool: str, arguments: dict) -> list[tuple[str, str, tuple]]:
        """前缀规则表达不了的语义判定，返回 [(verb, reason, key)]。

        为什么需要这一层：words_match 是【位置 0 前缀匹配】，而有一类危险是
        "任意位置出现某个词"——`find . -delete`、`curl … | sh`、`mv /etc/hosts`、
        `tee /etc/passwd`。把它们也塞进 BUILTIN_RULES 当模式词是错的（前缀匹配
        表达不了），所以单列这一层。每条都带固定的 rule 键（不是虚拟键），
        这样「本会话内允许」能记账、ask 恢复时 overlay 找得到它。
        """
        if tool != "run_bash":
            return []
        command = str(arguments.get("command") or "")
        hits: list[tuple[str, str, tuple]] = []
        try:
            segments = split_segments(command)   # 用未剥离的段：管道/删除标志要看原形
        except CommandParseError:
            return []                            # 解析失败已由 _builtin_verdict 转 ask
        ws = str(self.workspace)
        for seg in segments:
            if not seg:
                continue
            head = seg[0].split("/")[-1]
            rest = seg[1:]
            # a) `… | sh` / `… | bash`：把上游输出直接喂进 shell 执行（curl|sh 式）
            if head in _SHELL_C:
                key = ("run_bash", "<pipe-to-shell>")
                hits.append((ASK, f"把管道输入交给 shell 执行（| {head}）："
                                  "上游内容会被当命令跑", key))
            # b) find 的 -delete / -exec rm：删文件，但命令词是 find
            if head == "find" and any(t in ("-delete", "-exec", "-execdir") for t in rest):
                key = ("run_bash", "find * -delete")
                hits.append((ASK, "find 带 -delete/-exec：会在遍历中删除或执行命令", key))
            # c) 目标在工作区之外的写出类命令（mv / cp / tee / ln / install）。
            #    只认绝对路径：相对路径的越界由执行层 _resolve 兜（这里不重复
            #    判断，避免两处边界漂移）。self.workspace 是 resolve 过的绝对
            #    路径，前缀比对用字符串即可。
            if head in ("mv", "cp", "tee", "ln", "install") and any(
                    t.startswith("/") and not (t == ws or t.startswith(ws + "/"))
                    for t in rest if not t.startswith("-")):
                key = ("run_bash", "<write-outside-workspace>")
                hits.append((ASK, f"{head} 的目标路径在工作区之外：会写出工作区边界", key))
            # d) python -c / perl -e / node -e：整段脚本藏在参数里，静态审查看不见。
            #    但【不能一律拦】——`python3 -c "import demo; print(demo.add(2,2))"`
            #    是模型验证改动最常用的手段，每次弹卡会把人逼去 yolo 模式（更糟）。
            #    只查真正的破坏性调用：删文件/改权限/起进程/覆盖写。
            if head in ("python", "python3", "perl", "ruby", "node") and any(
                    t in ("-c", "-e", "--eval", "-pe") for t in rest):
                script = " ".join(t for t in rest if not t.startswith("-"))
                if _DESTRUCTIVE_SCRIPT.search(script):
                    key = ("run_bash", "<inline-script-destructive>")
                    hits.append((ASK, f"{head} 内联脚本里含破坏性调用"
                                      f"（rmtree/unlink/os.system/subprocess/覆盖写）", key))
        return hits

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
        try:
            bridge = dict(self.bridge_roots_loader()) if self.bridge_roots_loader else {}
        except Exception as e:
            # 桥接根解析失败（会话未知等）不放大权限：按无桥接继续——前缀会
            # 落到越界拒绝，与执行层的空表行为一致。
            log.warning("桥接根解析失败，按无桥接判定：%s", e)
            bridge = {}
        base = _builtin_verdict(tool, arguments, self.workspace, bridge)
        if base.verb == ASK:
            # 工具级 ask（解析失败）同样可被本轮决定 / 会话记忆吸收——
            # 不吸收的话，恢复后的重新判定会原样再问一次，回合永远走不出等待
            key = base.rule_key
            if overlay and key in overlay:
                base = overlay[key]
            elif remembered.get(key) == ALLOW:
                base = Verdict(ALLOW, f"{base.reason}（本会话已允许）", key)
        matched.append((base.verb, base.reason, base.rule_key))
        # 语义层（位置无关的高危判定：管道喂 shell、find -delete、越界写出、
        # 内联脚本）：与前缀规则同等参与「最严者胜」，同样可被本轮 overlay /
        # 会话记忆吸收——否则用户点了「本会话内允许」，恢复重判会被它原样再拦，
        # 回合永远走不出等待。
        semantic = [(v, r, k) for v, r, k in self._semantic_asks(tool, arguments)
                    for (v, r, k) in self._absorb(overlay, remembered, v, r, k)]
        matched.extend(semantic)
        # 整工具 allow（用户规则 pattern=None，reason/键形如 ("run_bash","")）是
        # 一个显式的「这个工具我全权托付」决定，优先级高于【内置的】位置规则与
        # 语义规则——否则用户写了 allow 却仍被每一条内置 ask 拦，等于该规则失效。
        # 但绝不越过 deny：最严者胜对 deny 依然成立（下面循环的第一个 verb）。
        whole_tool_allow = any(k and k[0] == tool and not k[1]
                               for v, _, k in matched if v == ALLOW)
        if whole_tool_allow and not any(v == DENY for v, _, _ in matched):
            return Verdict(ALLOW, "该工具已整工具放行（用户规则）", None)
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
        if mode == "restricted" and tool == "run_bash":
            # 受限模式：白名单判定。用户 allow 规则点名的命令模式作为白名单
            # 的【附加条目】参与逐段匹配——而不是整体跳过白名单：整体跳过会
            # 让 "allow curl" 连 "curl x | sh" 的第二段一起放进（管道右侧
            # 从未被允许过）。仅整工具 allow（pattern=None）才完全放行。
            # 内置高危 ask 清单不受影响（最严者胜，rm -rf 即便在名单外也仍走 ask）。
            user_allows = {k[1] for v, _, k in matched
                           if v == ALLOW and k and k[0] == "run_bash"}
            if "" not in user_allows:
                return self._restricted_bash_verdict(
                    str(arguments.get("command") or ""), overlay, remembered,
                    extra_patterns=tuple(p for p in user_allows if p))
        return Verdict(ALLOW, "允许", None)

    def _restricted_bash_verdict(self, command: str, overlay: dict | None,
                                 remembered: dict,
                                 extra_patterns: tuple = ()) -> Verdict:
        """restricted 模式下 run_bash 的白名单判定（check 末段调用）。

        硬闸与语义反转，按序：
        1. 命令串含 $ 或反引号 → ask（固定键）。shlex 把它们当普通字符，词
           匹配看不见"展开后的世界"：`ls `whoami`` 的第二个 token 就是
           "`whoami`"，"ls" 前缀照样命中——白名单下这是最大的逃逸面，必须在
           词匹配之前整体拦下（双引号内的 $ 同样展开，不做引号区分，保守）。
        2. 解析失败 → ask（与 confirm 共用 _PARSE_FAIL_KEY）。
        3. 每一段都必须命中白名单（any→all 的语义反转）；段内含操作符字符的
           token（>>、2>&1 等切段残留）直接不通过——重定向与描述符操作不在
           白名单世界，其目标路径也就无从校验；> 本身是段边界，重定向目标
           会成为新段首词、命中不了任何名单，双重兜底。
        4. 全部命中 → allow。
        extra_patterns 是用户 allow 规则点名的附加命令模式（check 传入）。
        两类 ask 用固定键，可被卡片决定（overlay）与会话记忆吸收——用户点过
        「本会话内允许」后，同类命令不再问。
        """
        if "`" in command or "$" in command:
            return self._restricted_ask(
                _EXPANSION_KEY,
                "受限模式：命令包含 $ 或反引号（shell 展开无法静态审查）",
                overlay, remembered)
        try:
            segments = split_segments(command)
        except CommandParseError as e:
            return self._restricted_ask(_PARSE_FAIL_KEY, str(e), overlay, remembered)
        operator_chars = ";&|<>()"
        for seg in segments:
            if any(tok and any(c in tok for c in operator_chars) for tok in seg):
                return self._restricted_ask(
                    _NOT_ALLOWLISTED_KEY,
                    "受限模式：命令含重定向/管道/子 shell 残留，不在白名单内",
                    overlay, remembered)
            if not _allowlist_hit(seg, self.workspace, extra_patterns):
                return self._restricted_ask(
                    _NOT_ALLOWLISTED_KEY,
                    "受限模式：命令不在 bash 白名单内（或参数越出工作区）",
                    overlay, remembered)
        return Verdict(ALLOW, "受限模式：白名单命令", None)

    @staticmethod
    def _restricted_ask(key: tuple, reason: str,
                        overlay: dict | None, remembered: dict) -> Verdict:
        """受限模式固定键 ask 的 overlay/会话记忆吸收（与 check 对工具级 ask
        的吸收逻辑同一语义，两个固定键共用一份实现）。"""
        if overlay and key in overlay:
            v = overlay[key]
            return Verdict(v.verb, v.reason, key, ask_keys=(key,))
        if remembered.get(key) == ALLOW:
            return Verdict(ALLOW, f"{reason}（本会话已允许）", key, ask_keys=(key,))
        return Verdict(ASK, reason, key, ask_keys=(key,))

    def _rule_hits(self, rule: Rule, tool: str, arguments: dict) -> bool:
        if rule.tool != tool:
            return False
        if rule.pattern is None:
            return True  # 整工具规则
        if tool != "run_bash":
            return False
        command = str(arguments.get("command") or "")
        try:
            # 用 normalize_segments（含透明前缀剥离）：`env rm -rf /` 若不剥离，
            # rm 不在位置 0，`rm -rf` 规则命中不了，命令零提示放行。
            segments = normalize_segments(command)
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

    def close_pending(self) -> int:
        """丢弃全部待确认请求，返回丢弃条数（回合收尾的 finally 调用）。

        为什么需要：pending 条目只在 apply_decisions 里 pop。open_requests →
        wait_all → apply_decisions 之间若发生异常（或生成器被提前关闭），条目
        会永久残留。残留的键会进 open_requests 的 claimed 集合，导致该规则键
        【从此不再弹确认卡】，并在 agent 侧被强制按拒绝处理——表现为"某个工具
        突然静默失效，怎么都不问、也永远不许"。回合收尾无条件清一次，把这类
        残留收敛在一个回合的生命周期内。

        幂等：没有 pending 时是空操作（正常路径每回合都会调到）。
        """
        with self._lock:
            n = len(self._pending)
            self._pending.clear()
        if n:
            log.warning("清理 %d 条未落定的权限确认请求（回合异常收尾/提前中止）", n)
        return n

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

    不静默失败：模型必须看到原因才能改道（换工作区内路径 / 换掉高危命令）。
    hint 明确【禁止原样重试】并给出可执行替代——旧文案「可换用其它方案」过于
    温和，实测会被无视：模型被拒后下一轮原样再调一次同一工具，白等一个确认
    超时（见 agent.py 的拒绝指纹检测，那是第二道闸）。
    """
    return json.dumps(
        {"ok": False, "error": f"权限拒绝: {verdict.reason}",
         "hint": "该调用已被拒绝，禁止用相同参数重试同一工具。请改道："
                 "①换等价的安全做法（改工作区内路径 / 换掉高危命令 / 换工具）；"
                 "②若确需用户授权，用一句话说明「需要你授权 X」后停止调用、结束本轮，"
                 "等待用户回复，不要重复发起同一个被拒请求。"},
        ensure_ascii=False,
    )
