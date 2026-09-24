"""
Coding 工具集
==============

让 Agent 能在"工作区"目录里读写本地代码、执行命令——这就是主流 coding agent
（Claude Code / Codex CLI 等）的基本工作方式：直接在本地文件夹中干活，不要求 git。

安全边界（初学阶段在本机直接干活的最低保障）：
1. 路径越界保护：所有文件操作被限制在工作区内，`../..` 逃逸直接报错；
   权限层（permissions.py）在调度前复用同一检查，把越界从「执行时报错」
   提前为「带原因的权限拒绝」；
2. 权限闸门：高危命令不再在本模块静默拦截——命令拆解、allow/deny/ask
   三态判定与用户确认全部收编进 permissions.py（拆段匹配能看清 `ls;rm -rf /`
   这类复合命令，也不会再误伤引号里的字符串）；
3. 超时与输出上限：防止命令卡死，也防止海量输出撑爆模型上下文。

真正的生产环境应把执行隔离进 Docker（见 docs/coding-agent-selection.md）。
"""

import json
import os
import re
import subprocess
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = _PROJECT_DIR / "workspace"

# 遍历时永远跳过的目录。除通用噪音外，还包含 data/ 下的运行时产物目录：
# browser-profiles（浏览器扩展源码含整张 TLD 域名表，曾被整读进历史后触发
# 供应商风控、全会话 400）、logs（服务端日志里全是报错原文，读入即自我污染）、
# backups / attachments / browser-shots / artifacts（二进制与大文件聚集地）。
# 这些目录对编码任务没有价值，产出物却极易撑爆上下文。
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode",
                "browser-profiles", "browser-shots", "attachments", "backups",
                "artifacts", "logs"}

MAX_READ_LINES = 2_000    # 单次 read_file 的行数上限：防止大文件一口气撑爆模型上下文。
                          # 行数而非字符数——行号展示与 offset/limit 分段都以"行"为轴，
                          # 两个口径统一后模型的心算成本最低。
MAX_OUTPUT_CHARS = 8_000  # 命令输出上限（保留末尾，报错通常在尾部）
MAX_GREP_MATCHES = 50
BASH_TIMEOUT = 30

# 行号栏宽度（右对齐空格填充）：4 位足够容纳绝大多数源文件；行号与正文之间
# 用制表符分隔。行号化是给 apply_patch 铺路的——锚定成功率直接取决于模型
# 能否精确引用文件原文（带行号的输出让"引用第几行"有据可查）。
_READ_LINE_WIDTH = 4


def format_numbered_lines(lines: list[str], start: int = 1) -> str:
    """把源码行渲染成「右对齐行号 + 制表符 + 原文」的统一格式。

    行号从 start（1 起）编号。这是 read_file 与 grep 输出的共同格式（纯函数，
    供单测直接断言）。为什么必须行号化：apply_patch 是锚定式编辑，模型的
    search 文本要落到具体行上；没有行号的读回，模型只能"凭印象"定位，
    锚定失败率显著上升，还容易在多轮阅读后把两次看到的行混在一起。
    """
    width = max(_READ_LINE_WIDTH, len(str(start + max(len(lines) - 1, 0))))
    return "\n".join(f"{i:>{width}}\t{text}" for i, text in enumerate(lines, start))


def _ok(payload: dict) -> str:
    """成功信封：统一补上 ok:true，工具实现只写业务字段。"""
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str, hint: str = "") -> str:
    """失败信封：{ok:false, error, hint?}。hint 是给模型的下一步建议——
    错误只说明"失败了"，hint 才能让模型改道（换路径/换工具/先侦察），
    而不是原样重试同一命令。"""
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工作区与路径安全
# ---------------------------------------------------------------------------

_WORKSPACE_SEED = {
    "README.txt": "这是 Agent 的工作区目录。你可以直接对 Agent 说：\n"
                  "  「运行 demo.py，它有个 bug，2+3 应该等于 5，请修复并验证」\n"
                  "  「在 workspace 里新建一个猜数字小游戏」\n",
    "demo.py": '# 练习用：里面有一个故意的 bug，让 Agent 修复它并运行验证\n'
               'def add(a, b):\n'
               '    return a - b  # TODO: 这里有个 bug\n'
               '\n'
               'if __name__ == "__main__":\n'
               '    print("2 + 3 =", add(2, 3))\n',
}


def prepare_workspace(ws: str | Path | None = None) -> Path:
    """解析工作区根目录并确保存在，返回绝对路径。

    优先级：显式传入（会话自己的工作区）> .env 的 WORKSPACE_DIR > 项目 workspace/。
    工作区曾经是全局唯一的环境变量——任何一个用户切换，所有会话立即跟着变，
    并发生成的两个 Agent 会互相踩对方目录；现在每个任务解析出自己的路径，
    通过 ToolContext 注入到每次工具调用（见 tools.py），互不可见。
    """
    if ws:
        target = Path(ws).expanduser().resolve()
    else:
        target = Path(os.environ.get("WORKSPACE_DIR") or DEFAULT_WORKSPACE).expanduser().resolve()
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        # 只给内置默认工作区放练习文件；用户自选的目录保持原样，别往里塞东西
        if target == DEFAULT_WORKSPACE:
            for name, content in _WORKSPACE_SEED.items():
                (target / name).write_text(content, encoding="utf-8")
    return target


def _ws(ctx) -> Path:
    """工具执行时的工作区：来自注入的 ToolContext，缺省回落到默认工作区。"""
    return prepare_workspace(getattr(ctx, "workspace", None) if ctx is not None else None)


def _resolve(path: str, ws: Path) -> Path:
    """把（相对工作区的）路径解析成绝对路径，并拦截越界访问。

    这是本模块最重要的一道防线：resolve() 消解掉 ../ 和符号链接之后，
    再校验目标必须仍在工作区之内。ws 是【本次调用】的工作区，由 ctx 注入。

    附件现在直接落进工作区的 .coding-agent/attachments/（见 db._session_attach_root），
    所以它们是工作区内的普通文件——无需任何特殊放行，越界校验保持最严。
    """
    if not path or not isinstance(path, str):
        raise ValueError("path 不能为空")
    target = (ws / path).resolve()
    if target != ws and ws not in target.parents:
        raise ValueError(f"路径越界：{path} 位于工作区之外，Agent 只能访问工作区内的文件")
    return target


# ---------------------------------------------------------------------------
# 六个工具：read_file / write_file / apply_patch / list_dir / grep / run_bash
# ---------------------------------------------------------------------------

# 六个工具的 ctx 形参不进 schema（模型看不到），由 execute_tool 在执行时注入——
# 模型只该决定"操作哪个相对路径"，"在哪个工作区里操作"是服务端的会话属性。

def _err(msg: str, hint: str = "") -> str:
    """失败信封：{ok:false, error, hint?}。hint 是给模型的下一步建议——
    错误只说明"失败了"，hint 才能让模型改道（换路径/换工具/先侦察），
    而不是原样重试同一命令。"""
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


def read_file(path: str, offset: int = 1, limit: int = 0, ctx=None) -> str:
    """读取文件，返回带行号的原文（apply_patch 锚定的取材来源）。

    offset/limit（均按 1 起的行号轴）：offset 是起始行，limit 是最多读几行
    （0 = 不限）。超过 MAX_READ_LINES 行自动截断，并在尾部明确提示用
    offset/limit 读后续段——截断提示不是客套话：模型看不到的部分如果被
    当成"整个文件"，apply_patch 的锚定与 write_file 的整文件覆盖都会误伤。
    """
    p = _resolve(path, _ws(ctx))
    if not p.exists():
        return _err(f"文件不存在: {path}", "先 list_dir 查看工作区的真实目录结构，确认路径拼写")
    if p.is_dir():
        return _err(f"{path} 是目录，请用 list_dir 查看")
    text = p.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    total = len(all_lines)

    # offset/limit 归一：容忍字符串数字/0/负数/垃圾值（模型偶尔传 0 表示"从头"，
    # 传"abc"则是幻觉参数）——一律宽容归一，绝不因参数问题打断读取任务
    try:
        start = max(1, int(offset or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        limit = max(0, int(limit or 0))
    except (TypeError, ValueError):
        limit = 0
    end = start + limit - 1 if limit > 0 else total
    # 行数兜底与显式 limit 同一通道：先按 end 切片，再看是否发生截断
    hard_end = min(end, start - 1 + MAX_READ_LINES)
    window = all_lines[start - 1:hard_end]

    if not window:
        return _err(f"读取区间为空：文件共 {total} 行，请求起始行 {start}"
                    + (f"（超出了文件末尾）" if start > total else ""),
                    f"用 offset=1 重读，或先用 grep 定位目标内容所在的行号")

    # 截断判定：显式 limit 截短、行数上限截短，都要让模型知道"下面还有"
    truncated = hard_end < total or (0 < limit <= len(window) and start - 1 + limit < total)
    content = format_numbered_lines(window, start)
    if truncated:
        next_offset = start + len(window)
        content += (f"\n...[已截断：文件共 {total} 行，本次显示第 {start}-{start + len(window) - 1} 行。"
                    f"用 offset/limit 读取后续段：传 offset={next_offset}。"
                    f"修改前务必读到目标位置的真实原文]")
    return _ok({"path": path, "result": content,
                "total_lines": total, "shown": [start, start + len(window) - 1]})


def write_file(path: str, content: str, ctx=None) -> str:
    """新建文件，或整文件覆盖。修改已有文件的局部内容请优先用 apply_patch。"""
    p = _resolve(path, _ws(ctx))
    if p.is_dir():
        return _err(f"{path} 是目录，不能当作文件写入",
                    "换一个文件路径；若想看该目录里已有什么，用 list_dir")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return _ok({"path": path,
                "result": f"已写入 {path}（{len(content.encode('utf-8'))} 字节 / {len(content.splitlines())} 行）",
                "bytes": len(content.encode("utf-8")), "lines": len(content.splitlines())})


def _strip_line_no(line: str) -> str:
    """剥离模型可能从带行号输出里原样复制来的行号栏（"  17\\t"）。

    read_file 的输出带行号，模型偶尔会把行号一起写进 search——这是行号化
    带来的新失败模式，在这里统一兜掉（只剥行首"空白+数字+制表符"形态，
    正文里正常的代码不受影响）。"""
    return re.sub(r"^\s*\d+\t", "", line)


def _anchored_hit(text: str, s: str) -> int:
    """s 在 text 中【行首锚定】的唯一命中位置；非唯一或未命中返回 -1。

    为什么锚定到行首而不能用裸子串：剥离行号栏后的 search 常丢掉前导缩进
    （行号栏连同其前的空白一起剥离），裸子串会命中"原文前导空格的尾部"
    （"  return..." 匹配进 "    return..." 的后两个空格），把错误缩进
    原样写进文件。要求命中位置是行首（0 或紧跟 \\n）才能接受。"""
    if not s:
        return -1
    hits = []
    start = 0
    while True:
        idx = text.find(s, start)
        if idx < 0:
            break
        if idx == 0 or text[idx - 1] == "\n":
            hits.append(idx)
        start = idx + 1
    return hits[0] if len(hits) == 1 else -1


def _reindent_by_position(replace_lines: list[str], file_indents: list[str]) -> list[str]:
    """救援路径的缩进重建：完全以【文件被替换段】的行位置为准。

    replace 第 j 行继承 file_indents[min(j, len-1)] 的缩进（模型在救援场景下
    的缩进已证明不可信——可能是错位数、可能被行号栏污染，一律不采）；
    超出被替换段行数的新增行继承上一行的缩进、再叠加它自己相对【新增段首行】
    的额外缩进（新增嵌套块保层级）。空行不补缩进。
    """
    out = []
    base_extra = None
    for j, ln in enumerate(replace_lines):
        if not ln.strip():
            out.append("")
            continue
        base = file_indents[min(j, len(file_indents) - 1)]
        if j < len(file_indents):
            out.append(base + ln.lstrip())
        else:
            # 新增行：相对第一个新增行的缩进差保留，贴在继承基线上
            if base_extra is None:
                base_extra = len(ln) - len(ln.lstrip())
            delta = max(0, (len(ln) - len(ln.lstrip())) - base_extra)
            out.append(base + " " * delta + ln.lstrip())
    return out


def apply_patch(path: str, search: str, replace: str, ctx=None) -> str:
    """锚定式编辑（Aider 的 SEARCH/REPLACE 思路）：
    在文件里找 search 原文，替换为 replace。

    失败模式都把原因返回给模型让它自行纠正，绝不静默失败：
    - 找不到原文（模型凭记忆写、缩进不一致）→ 提示先 read_file 核对（行号
      输出就是为了这一步——锚定文本必须逐字符来自刚读到的原文）；
    - 原文出现多次（锚点不唯一）→ 提示增加上下文行数。

    两层兜底（提高一次成功率，但不放松"改对"的标准）：
    1. 行号栏剥离：search 每行先剥掉可能误粘的 "  17\\t" 行号栏再逐字符找；
    2. 锚定救援：逐字符仍找不到时，退化比较"每行 strip 掉行首缩进与行尾
       空白"的版本。缩进数错、行尾空格差异这类高频小错不再整次失败；
       replace 由 _reindent 按原文缩进重建（保层级不抹平）。救援条件严格：
       strip 后仍必须命中，且 search 的 strip 版在全文唯一——多义一律拒绝，
       宁失败不错改。救援命中的返回里带 note，模型能知道实际替换位置。
    """
    p = _resolve(path, _ws(ctx))
    if not p.exists():
        return _err(f"文件不存在: {path}", "先 list_dir 查看工作区的真实目录结构，确认路径拼写")
    text = p.read_text(encoding="utf-8", errors="replace")
    if not search.strip():
        return _err("search 不能为空", "把要替换的原文（取自 read_file 的输出）写进 search")

    # 三层匹配，逐层降级，每层都要求"能改才改"：
    # ① 原样逐字符【行首锚定】（模型照抄原文的正常路径）——锚定行首是因为
    #    裸子串会命中"前导空格的尾部"（"  return..." 匹配进 "    return..."
    #    的后两个空格），把错误缩进原样写进文件；
    # ② 剥行号栏后行首锚定逐字符（模型把 "  17\\t" 一起复制了进来）；
    # ③ 锚定救援：每行 strip 缩进/行尾空白后按行比较，唯一命中才动手
    #   （缩进数错、行尾空格差异；replace 由 _reindent 保层级重建）。
    # 任何一层都必须唯一命中——多义一律拒绝，宁失败不错改。
    count = text.count(search)
    pos = text.find(search) if count == 1 else -1
    if pos >= 0 and (pos == 0 or text[pos - 1] == "\n"):
        new_text = text[:pos] + replace + text[pos + len(search):]
        how = None
        first_line = text[:pos].count("\n") + 1
    else:
        stripped_search = "\n".join(_strip_line_no(ln) for ln in search.splitlines())
        stripped_replace = "\n".join(_strip_line_no(ln) for ln in replace.splitlines())
        pos = _anchored_hit(text, stripped_search) if stripped_search != search else -1
        if pos >= 0:
            new_text = text[:pos] + stripped_replace + text[pos + len(stripped_search):]
            how = None
            first_line = text[:pos].count("\n") + 1
        else:
            # 救援比较用剥过行号栏的版本：strip() 只去空白，"  2\treturn…"
            # 原样 strip 会留下 "2\treturn…"，永远配不上文件行
            s_lines = stripped_search.splitlines()
            file_lines = text.splitlines()
            s_stripped = [ln.strip() for ln in s_lines]
            hits = [i for i in range(len(file_lines) - len(s_lines) + 1)
                    if [ln.strip() for ln in file_lines[i:i + len(s_lines)]] == s_stripped]
            if len(hits) != 1:
                if not any(ln.strip() for ln in s_lines):
                    return _err("search 不能全为空行", "把要替换的原文（取自 read_file 的输出）写进 search")
                if len(hits) > 1:
                    return _err(f"search 原文在文件中出现了 {len(hits)} 处（按内容去缩进比较），无法定位",
                                "在 search 中多包含几行上下文（可含行号输出里相邻行的原文），使其在文件中唯一")
                return _err("未找到要替换的原文：search 必须与文件内容逐字符一致（包括空格和缩进）",
                            "先 read_file 该文件，从刚返回的带行号原文里逐字符复制 search，禁止凭记忆书写")
            i = hits[0]
            # 缩进完全以文件被替换段为准重建（模型缩进在救援场景下不可信）
            replace_lines = stripped_replace.splitlines()
            file_indents = [
                fl[:len(fl) - len(fl.lstrip())] if fl.strip() else ""
                for fl in file_lines[i:i + len(s_lines)]
            ]
            fixed_lines = _reindent_by_position(replace_lines, file_indents)
            new_lines = file_lines[:i] + fixed_lines + file_lines[i + len(s_lines):]
            new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
            how = "锚定救援：search 与原文存在缩进/行尾空白差异，已按原文件逐行缩进对齐替换"
            first_line = i + 1
    p.write_text(new_text, encoding="utf-8")
    payload = {
        "path": path,
        "result": f"已修改 {path}（第 {first_line} 行起 +{len(replace.splitlines())} / −{len(search.splitlines())} 行）",
        "added": len(replace.splitlines()), "removed": len(search.splitlines()),  # 供前端显示 +N −N
        "matched_lines": [first_line, first_line + len(search.splitlines()) - 1],
    }
    if how:
        payload["note"] = how
    return _ok(payload)


def list_dir(path: str = ".", ctx=None) -> str:
    p = _resolve(path, _ws(ctx))
    if not p.is_dir():
        return _err(f"{path} 不是目录",
                    "想看的是文件内容的话用 read_file；确认路径是否写对可先 list_dir 根目录")
    entries = []
    for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))[:200]:
        if e.is_dir():
            entries.append({"name": e.name + "/", "type": "dir"})
        else:
            entries.append({"name": e.name, "type": "file", "bytes": e.stat().st_size})
    return _ok({"path": path, "result": entries})


def grep(pattern: str, path: str = ".", ctx=None) -> str:
    """在工作区内做正则搜索：匹配行渲染成与 read_file 相同的
    「行号+制表符」格式（file 字段与 line 字段单独给出）。path 可为目录或单文件。"""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return _err(f"正则表达式不合法: {e}", "简化正则再试，或把特殊字符加 \\ 转义")
    ws = _ws(ctx)
    root = _resolve(path, ws)
    if root.is_file():
        files = [root]  # os.walk 对文件路径一次都不迭代，直接传会静默返回 0 匹配
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            files.extend(Path(dirpath) / name for name in filenames)
    matches = []
    for fp in files:
        try:
            if fp.stat().st_size > 1_000_000:
                continue
            with open(fp, "rb") as probe:  # 二进制文件（含 \0）跳过
                if b"\0" in probe.read(1024):
                    continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        matches.append({
                            "file": str(fp.relative_to(ws)),
                            "line": lineno,
                            "text": format_numbered_lines([line.rstrip("\n")], lineno),
                        })
                        if len(matches) >= MAX_GREP_MATCHES:
                            return _ok({"path": path, "result": matches, "total": len(matches),
                                        "note": f"已达 {MAX_GREP_MATCHES} 条上限，请缩小搜索范围（收窄 path 或换更具体的 pattern）"})
        except OSError:
            continue
    return _ok({"path": path, "result": matches, "total": len(matches)})


def run_bash(command: str, ctx=None) -> str:
    """在工作区目录里执行 shell 命令（cwd 锁定工作区、30 秒超时、输出截断）。

    高危判定不在这里做：本函数只管执行。allow/deny/ask 三态判定（含命令
    拆解与用户确认）在调度前的权限闸门（permissions.py）完成——那里能把
    `ls;rm -rf /` 拆开看、也能让 `rm -rf /tmp/test` 这类操作先过问用户。
    到达这里 = 已获放行。
    """
    if not command or not isinstance(command, str):
        return _err("command 不能为空", "把要执行的命令写进 command 参数")
    try:
        proc = subprocess.run(
            command, shell=True, cwd=_ws(ctx),
            capture_output=True, text=True, timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _err(f"命令执行超过 {BASH_TIMEOUT} 秒被终止",
                    "拆成更小的步骤分次执行，或给命令加超时/分页控制（如 head 限制输出）")
    stdout = (proc.stdout or "")[-MAX_OUTPUT_CHARS:]
    stderr = (proc.stderr or "")[-MAX_OUTPUT_CHARS // 2:]
    combined = (stdout + ("\n[stderr]\n" + stderr if stderr else "")).strip() or "（无输出）"
    # 输出只进 result 一个字段（已含 stdout 与 [stderr] 段，各自截断过）。
    # 之前 payload 同时带 stdout/stderr/result 三份——同一段输出在工具结果里
    # 存两遍，而工具结果一旦进历史就会随之后每轮请求重复携带，纯浪费。
    payload = {"exit_code": proc.returncode, "result": combined}
    if proc.returncode != 0:
        # 非零退出走失败信封：模型看 ok 就能分流；原始输出保留在 result 里
        # 供定位（命令失败不是工具失败，输出本身就是最重要的错误信息）
        payload["error"] = f"命令退出码 {proc.returncode}"
        payload["hint"] = "先读 stderr 定位原因再调整命令；不要原样重试同一条命令"
        return json.dumps({"ok": False, **payload}, ensure_ascii=False)
    return _ok(payload)


# ---------------------------------------------------------------------------
# schema 与注册表（与 tools.py 相同的三段式）
# ---------------------------------------------------------------------------

CODE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内一个文本文件，返回带行号的原文（格式 \"  17\\tdef foo():\"，行号+制表符+原文，行号从 1 起）。"
                           "什么时候用：修改任何文件之前、需要确认某段代码的真实内容时。什么时候不用：找某个函数在哪用 grep 更快；"
                           "看目录结构用 list_dir。"
                           "关键纪律：apply_patch 的 search 文本必须逐字符取自本工具刚返回的原文（不含行号栏），"
                           "禁止凭记忆书写——记错一个缩进就锚定失败。"
                           "示例：read_file {\"path\": \"src/main.py\"} 读全文件（超过 2000 行会截断并提示后续 offset）；"
                           "read_file {\"path\": \"src/main.py\", \"offset\": 120, \"limit\": 40} 只读第 120–159 行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的路径，如 'src/main.py'"},
                    "offset": {"type": "integer", "description": "起始行号（1 起），默认 1。配合 limit 分段读大文件"},
                    "limit": {"type": "integer", "description": "最多读的行数，默认 0 = 读到文件尾或 2000 行上限"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "新建文件，或用 content 整文件覆盖一个已有文件。"
                           "什么时候用：新建文件；内容需要整体重写（改动的行数超过文件一半）时。"
                           "什么时候不用：改已有文件的几行、一个函数——用 apply_patch（省 token，且不会误伤文件其它部分）。"
                           "覆盖前若没读过该文件，必须先 read_file，否则会抹掉你不了解的内容。"
                           "示例：write_file {\"path\": \"utils.py\", \"content\": \"def helper():\\n    return 1\\n\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的路径（新文件的父目录会自动创建）"},
                    "content": {"type": "string", "description": "完整的文件内容（覆盖式写入，不是追加）"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "精确修改已有文件的一小段：在文件中查找 search 原文，整体替换为 replace。"
                           "这是改代码的首选工具（改几行/改函数签名/修 bug 都用它），只有新建文件或整体重写才用 write_file。"
                           "硬性要求：search 必须与文件当前内容逐字符一致（含缩进），且在文件中唯一——"
                           "所以 search 文本必须逐字符复制自刚刚 read_file 返回的带行号原文，没读过文件不许调用本工具；"
                           "锚点不唯一时在 search 里多带几行上下文。"
                           "示例：apply_patch {\"path\": \"calc.py\", \"search\": \"def add(a, b):\\n    return a - b\", "
                           "\"replace\": \"def add(a, b, c=0):\\n    return a + b + c\"}。"
                           "失败时会返回原因与 hint（如「先 read_file 核对」），按 hint 改正，不要原样重试。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的路径"},
                    "search": {"type": "string", "description": "要被替换的原文（逐字符精确匹配，可含多行；取自刚 read_file 到的内容）"},
                    "replace": {"type": "string", "description": "替换后的新内容（整段给出，含未变的首尾行也可）"},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作区内某个目录的内容（文件名、类型、大小），每个文件名只列一层。"
                           "什么时候用：接到新任务先看一眼工作区结构；read_file 报「文件不存在」时核对真实路径。"
                           "什么时候不用：找代码符号（函数/类/变量名）用 grep；看文件内容用 read_file。"
                           "示例：list_dir {\"path\": \"src\"}；根目录直接 list_dir {}。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对工作区的目录路径，默认列出根目录"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "在工作区文件内容中做正则搜索，返回全部匹配行（file + line + 带行号原文），最多 50 条。"
                           "什么时候用：定位函数/类的定义与所有调用点、找报错信息来源、找配置项——先 grep 定位再 read_file 精读。"
                           "什么时候不用：已经知道文件和大概位置时直接 read_file 的 offset/limit；浏览目录结构用 list_dir。"
                           "pattern 是 Python 正则；path 可限定目录或单个文件。"
                           "示例：grep {\"pattern\": \"def \\\\w+\\\\(\", \"path\": \"src\"} 列出 src 下所有函数定义行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式，如 'add\\\\(' 查 add 的调用点"},
                    "path": {"type": "string", "description": "限定搜索的目录或文件（可精确到单个文件），默认整个工作区"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "在工作区目录里执行一条 shell 命令（30 秒超时，输出超长时保留尾部 8000 字符）。"
                           "什么时候用：跑程序/测试验证改动、git status、pip list 这类检查。"
                           "什么时候不用：能被专用工具做的事——读文件用 read_file（不要 cat，前者带行号且受限工作区内）、"
                           "搜索用 grep（不要 grep 命令，专用的会返回结构化结果）、改文件绝不走 sed/echo 重定向。"
                           "调用前先用一句话说明这条命令的目的；预期输出很长时主动接 head/tail 缩窄。"
                           "高危命令（rm -rf、sudo、git push 等）会先弹卡请求用户确认，拒绝会带原因返回。"
                           "示例：run_bash {\"command\": \"python3 demo.py\"}；长输出如 run_bash {\"command\": \"ls -la | head -30\"}。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
                "required": ["command"],
            },
        },
    },
]

CODE_TOOL_REGISTRY = {
    "read_file": read_file,
    "write_file": write_file,
    "apply_patch": apply_patch,
    "list_dir": list_dir,
    "grep": grep,
    "run_bash": run_bash,
}

# 工具元数据 read_only：该工具是否只读（不改动工作区/会话的任何状态）。
# read_file / list_dir / grep 只打开文件读 → True，可被 Agent 安全地并行执行；
# write_file / apply_patch 真的会写、run_bash 是任意 shell 命令（无法证明它
# 不写，哪怕看起来只是 cat/ls）→ False，必须串行。
# tools.py 会把这份表并入全局 TOOL_READ_ONLY，Agent 的分组调度以它为准。
CODE_TOOL_READ_ONLY = {
    "read_file": True,
    "write_file": False,
    "apply_patch": False,
    "list_dir": True,
    "grep": True,
    "run_bash": False,
}
