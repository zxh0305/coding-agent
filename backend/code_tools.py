"""
Coding 工具集
==============

让 Agent 能在"工作区"目录里读写本地代码、执行命令——这就是主流 coding agent
（Claude Code / Codex CLI 等）的基本工作方式：直接在本地文件夹中干活，不要求 git。

三道安全边界（初学阶段在本机直接干活的最低保障）：
1. 路径越界保护：所有文件操作被限制在工作区内，`../..` 逃逸直接报错；
2. 命令黑名单：`sudo`、`rm -rf /` 等高危命令直接拒绝，并把原因反馈给模型；
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

# 遍历时永远跳过的目录
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}

# 高危命令黑名单（粗粒度兜底，不是安全边界；重要数据请自行备份或后续上 Docker）
_DANGEROUS_RE = re.compile(
    r"\bsudo\b|\bshutdown\b|\breboot\b|\brm\s+(-[a-z]*r[a-z]*f|-rf)\b\s*[/~]|\bmkfs\b|\bdd\s+if=",
    re.IGNORECASE,
)

MAX_READ_CHARS = 40_000   # 单次读文件上限，防止撑爆上下文
MAX_OUTPUT_CHARS = 8_000  # 命令输出上限（保留末尾，报错通常在尾部）
MAX_GREP_MATCHES = 50
BASH_TIMEOUT = 30


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


def get_workspace() -> Path:
    """工作区根目录：Agent 的全部文件操作都被限制在这里。可用 .env 的 WORKSPACE_DIR 覆盖。"""
    ws = Path(os.environ.get("WORKSPACE_DIR") or DEFAULT_WORKSPACE).expanduser().resolve()
    if not ws.exists():
        ws.mkdir(parents=True, exist_ok=True)
        for name, content in _WORKSPACE_SEED.items():  # 首次创建时放两个练习文件（不覆盖已有）
            (ws / name).write_text(content, encoding="utf-8")
    return ws


def _resolve(path: str) -> Path:
    """把（相对工作区的）路径解析成绝对路径，并拦截越界访问。

    这是本模块最重要的一道防线：resolve() 消解掉 ../ 和符号链接之后，
    再校验目标必须仍在工作区之内。
    """
    if not path or not isinstance(path, str):
        raise ValueError("path 不能为空")
    ws = get_workspace()
    target = (ws / path).resolve()
    if target != ws and ws not in target.parents:
        raise ValueError(f"路径越界：{path} 位于工作区之外，Agent 只能访问工作区内的文件")
    return target


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 六个工具：read_file / write_file / apply_patch / list_dir / grep / run_bash
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return _err(f"文件不存在: {path}")
    if p.is_dir():
        return _err(f"{path} 是目录，请用 list_dir 查看")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n...[文件过长已截断，共 {len(text)} 字符；建议先用 grep 定位行号再配合 read_file 分段关注]"
    return json.dumps({"path": path, "content": text}, ensure_ascii=False)


def write_file(path: str, content: str) -> str:
    """新建或整文件覆盖。修改已有代码请优先用 apply_patch（省 token、不易误伤）。"""
    p = _resolve(path)
    if p.is_dir():
        return _err(f"{path} 是目录，不能当作文件写入")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return json.dumps({
        "ok": True, "path": path,
        "bytes": len(content.encode("utf-8")), "lines": len(content.splitlines()),
    }, ensure_ascii=False)


def apply_patch(path: str, search: str, replace: str) -> str:
    """锚定式编辑（Aider 的 SEARCH/REPLACE 思路）：
    在文件里找 search 原文，替换为 replace。

    失败模式只有两种，且都把原因返回给模型让它自行纠正：
    - 找不到原文（模型记错了内容）→ 提示先 read_file 核对；
    - 原文出现多次（锚点不唯一）→ 提示增加上下文行数。
    """
    p = _resolve(path)
    if not p.exists():
        return _err(f"文件不存在: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(search)
    if count == 0:
        return _err("未找到要替换的原文：search 必须与文件内容逐字符一致（包括空格和缩进）。请先 read_file 核对后再试")
    if count > 1:
        return _err(f"search 原文在文件中出现了 {count} 次，无法定位。请在 search 中多包含几行上下文，使其唯一")
    p.write_text(text.replace(search, replace, 1), encoding="utf-8")
    return json.dumps({
        "ok": True, "path": path,
        "added": len(replace.splitlines()), "removed": len(search.splitlines()),  # 供前端显示 +N −N
    }, ensure_ascii=False)


def list_dir(path: str = ".") -> str:
    p = _resolve(path)
    if not p.is_dir():
        return _err(f"{path} 不是目录")
    entries = []
    for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))[:200]:
        if e.is_dir():
            entries.append({"name": e.name + "/", "type": "dir"})
        else:
            entries.append({"name": e.name, "type": "file", "bytes": e.stat().st_size})
    return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)


def grep(pattern: str, path: str = ".") -> str:
    """在工作区内做正则搜索（简化版 ripgrep）：返回 文件:行号: 内容。"""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return _err(f"正则表达式不合法: {e}")
    root = _resolve(path)
    ws = get_workspace()
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            fp = Path(dirpath) / name
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
                                "text": line.strip()[:200],
                            })
                            if len(matches) >= MAX_GREP_MATCHES:
                                return json.dumps({"matches": matches, "note": f"已达 {MAX_GREP_MATCHES} 条上限，请缩小范围"}, ensure_ascii=False)
            except OSError:
                continue
    return json.dumps({"matches": matches, "total": len(matches)}, ensure_ascii=False)


def run_bash(command: str) -> str:
    """在工作区目录里执行 shell 命令（cwd 锁定工作区、30 秒超时、输出截断）。"""
    if not command or not isinstance(command, str):
        return _err("command 不能为空")
    if _DANGEROUS_RE.search(command):
        return _err("命令被安全策略拒绝：涉及 sudo / 全盘删除等高危操作。请换用更精确、作用域更小的方式")
    try:
        proc = subprocess.run(
            command, shell=True, cwd=get_workspace(),
            capture_output=True, text=True, timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _err(f"命令执行超过 {BASH_TIMEOUT} 秒被终止。请拆成更小的步骤或加上超时控制")
    stdout = (proc.stdout or "")[-MAX_OUTPUT_CHARS:]
    stderr = (proc.stderr or "")[-MAX_OUTPUT_CHARS // 2:]
    return json.dumps({"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# schema 与注册表（与 tools.py 相同的三段式）
# ---------------------------------------------------------------------------

CODE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内某个文本文件的内容。修改文件前必须先读它，不要凭记忆猜内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对工作区的路径，如 'src/main.py'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "新建文件，或整体覆盖写入一个文件。修改已有文件的局部内容时优先用 apply_patch。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的路径"},
                    "content": {"type": "string", "description": "完整的文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "精确修改已有文件：在文件中查找 search 原文并替换为 replace。search 必须与文件内容逐字符一致（含缩进）且在文件中唯一；不确定内容时先 read_file。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的路径"},
                    "search": {"type": "string", "description": "要被替换的原文（逐字符精确匹配，可含多行）"},
                    "replace": {"type": "string", "description": "替换后的新内容"},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作区内某个目录的内容（文件名、类型、大小）。",
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
            "description": "在工作区文件内容中做正则搜索，返回 文件:行号:内容。找代码、找函数定义、找报错来源都用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "限定搜索的子目录，默认整个工作区"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "在工作区目录里执行 shell 命令（30 秒超时，输出截断）。用于运行程序、跑测试、做语法检查。高危命令会被拒绝。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的命令"}},
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
