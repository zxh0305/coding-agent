"""
Git 只读查询
============

给前端「Git 提交记录」浮窗供数据：列出提交、看某条提交的逐文件 diff、
判断"这条是不是我提交的"。全部是只读操作（log / show / config），不写盘、
不改仓库状态——因此不经权限闸门。

为什么单独一个模块而不是塞进 app.py：
1. 解析 git 输出（字段分隔、unified diff 拆行）是有状态机的纯逻辑，单独放
   便于写单测；app.py 只负责路由与鉴权；
2. 与 code_tools.py 的边界：那里是"给 Agent 用的工具"（有 schema、走闸门、
   输出面向模型），这里只服务前端浮窗，输出是给界面渲染的结构化数据。

字段分隔用 \x1f（ASCII 单元分隔符）而不是空格/制表符：提交信息里既有空格
又有换行，只有不可见控制字符能安全当分隔符。
"""

import re
import subprocess
from pathlib import Path

# git 命令的超时：log/show 是本地操作，正常毫秒级返回；给 10 秒足够，
# 卡住（如巨型仓库、损坏的索引）就放弃而不是拖死 HTTP 线程。
GIT_TIMEOUT = 10

# 单条提交最多返回的文件数、单个文件最多返回的 diff 行数：
# 巨型提交（自动生成的文件、依赖锁文件）的 patch 可能有几十万行，
# 整包塞给浏览器会卡死。截断并明确告知"还有更多"。
MAX_FILES_PER_COMMIT = 50
MAX_DIFF_LINES_PER_FILE = 800

UNIT = "\x1f"      # 字段分隔符（unit separator）
REC = "\x02"       # 记录前缀标记（STX）：给 --shortstat 的统计行定位边界


class GitError(Exception):
    """git 不可用 / 目录不是仓库 / 命令失败。message 面向用户展示。"""


def _run(ws: Path, args: list[str]) -> str:
    """在 ws 目录里跑一条 git 命令，返回 stdout；失败抛 GitError。

    不用 shell=True：参数以列表传入，天然免疫命令注入（提交 hash 等来自
    前端的值直接作为独立 argv 元素，不会被解释成 shell 语法）。
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(ws),
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
            errors="replace",  # 仓库里混入非 UTF-8 文件名时不至于整个失败
        )
    except FileNotFoundError:
        raise GitError("系统里没有找到 git 命令")
    except subprocess.TimeoutExpired:
        raise GitError(f"git 命令超过 {GIT_TIMEOUT} 秒未返回")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        msg = err[-1] if err else f"git 退出码 {proc.returncode}"
        raise GitError(msg)
    return proc.stdout


def is_repo(ws: Path) -> bool:
    """ws 是否位于一个 git 工作树内（含子目录）。"""
    try:
        out = _run(ws, ["rev-parse", "--is-inside-work-tree"])
    except GitError:
        return False
    return out.strip() == "true"


def identity(ws: Path) -> dict:
    """本机 git 身份，用于判断"哪些提交是我做的"。

    优先仓库级配置，回落到全局（--get 在两处都查）。缺失时返回空串——
    前端据空值把筛选器降级为"按作者分组"而不是硬判 mine。
    """
    def cfg(key: str) -> str:
        try:
            return _run(ws, ["config", "--get", key]).strip()
        except GitError:
            return ""
    return {"name": cfg("user.name"), "email": cfg("user.email")}


def repo_summary(ws: Path) -> dict:
    """仓库概览：当前分支、是否有未提交改动。浮窗顶部展示。"""
    try:
        branch = _run(ws, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    except GitError:
        branch = ""
    # 分离头指针时 --abbrev-ref 返回 "HEAD"，换成短 hash 更好认
    if branch == "HEAD":
        try:
            branch = "detached@" + _run(ws, ["rev-parse", "--short", "HEAD"]).strip()
        except GitError:
            branch = "detached"
    dirty = 0
    try:
        out = _run(ws, ["status", "--porcelain"])
        dirty = len([ln for ln in out.splitlines() if ln.strip()])
    except GitError:
        pass
    return {"branch": branch, "dirty": dirty}


def branches(ws: Path) -> dict:
    """本地分支列表 + 当前分支（供浮窗切换）。

    for-each-ref 的 %(refname:short) 给出分支名，%(HEAD) 标出当前分支（"*"）。
    只列本地分支：远端分支（origin/xxx）数量可能很多，且直接切过去会进
    分离头指针状态，对"看看代码"这个场景是噪声。
    """
    out = _run(ws, ["for-each-ref", "--format=%(HEAD)%(refname:short)",
                    "refs/heads/"])
    items, current = [], ""
    for line in out.splitlines():
        if not line.strip():
            continue
        mark, name = line[0], line[1:].strip()
        if not name:
            continue
        if mark == "*":
            current = name
        items.append({"name": name, "current": mark == "*"})
    # 当前分支排最前，其余按名字排序——常用项不用翻
    items.sort(key=lambda b: (not b["current"], b["name"]))
    return {"branches": items, "current": current}


def checkout(ws: Path, branch: str) -> dict:
    """切换到指定本地分支。

    这是本模块唯一的【写操作】（会改工作树的 HEAD 与文件）。因此：
    1. 分支名必须已在本地分支白名单里——不接受任意字符串，杜绝
       "git checkout <用户输入>" 被当作选项注入（如 -B 新建/覆盖分支）；
    2. 工作树有未提交改动时 git 自己会拒绝（冲突），错误原样抛给用户看；
    3. 不做 --force：绝不静默丢弃用户没提交的改动。
    """
    names = {b["name"] for b in branches(ws)["branches"]}
    if branch not in names:
        raise GitError(f"本地没有分支 {branch}")
    _run(ws, ["checkout", branch])
    return {"branch": branch}


def _shortstat(text: str) -> tuple[int, int]:
    """从 --shortstat 输出里抠出 (新增行数, 删除行数)。

    形如 " 3 files changed, 120 insertions(+), 8 deletions(-)"，两种
    计数都可能缺省（纯删 / 纯增），所以两个都要容错。
    """
    added = removed = 0
    m = re.search(r"(\d+) insertion", text)
    if m:
        added = int(m.group(1))
    m = re.search(r"(\d+) deletion", text)
    if m:
        removed = int(m.group(1))
    return added, removed


def log(ws: Path, limit: int = 30, offset: int = 0, author: str = "") -> dict:
    """提交列表（新→旧）。author 非空时只返回该作者的提交（按 email 精确匹配）。

    每条记录的字段用 \x1f 分隔：
      %H 完整 hash | %h 短 hash | %an 作者名 | %ae 作者邮箱
      | %aI ISO 时间 | %s 标题

    --shortstat 的统计行由 git 附加在每条记录【之后】，且落在格式串之外。
    所以记录边界不能靠"格式串末尾的分隔符"来切——那样统计行会归到下一段，
    每条都取到别人的统计（或取空）。改用 \x02 前缀标记：每条记录以它开头，
    其后的第一行是字段，剩下的就是本条自己的统计行。
    """
    fmt = REC + UNIT.join(["%H", "%h", "%an", "%ae", "%aI", "%s"])
    args = ["log", f"--pretty=format:{fmt}", "--shortstat",
            f"--max-count={limit}", f"--skip={offset}"]
    if author:
        args.append(f"--author={author}")
    out = _run(ws, args)

    me = identity(ws).get("email", "").lower()
    items = []
    for chunk in out.split(REC)[1:]:  # [0] 是首个标记之前的空串
        head, _, stat_text = chunk.partition("\n")
        parts = head.split(UNIT)
        if len(parts) < 6:
            continue
        full, short, an, ae, date, subject = parts[:6]
        added, removed = _shortstat(stat_text)
        items.append({
            "hash": full, "short": short,
            "author": an, "email": ae, "date": date, "subject": subject,
            "added": added, "removed": removed,
            "mine": bool(me) and ae.lower() == me,
        })
    return {"commits": items, "me": me, "identity": identity(ws)}


def _parse_patch(patch: str) -> list[dict]:
    """把 unified diff 正文拆成 [{path, added, removed, hunks:[...]}]。

    一个 hunk = {"header": "@@ … @@", "lines": [{t, text}]}，t ∈ ctx/del/add。
    解析规则极简：只认 "diff --git" / "+++ " / "@@" 三种行首，其余按前缀
    归类——git 的输出格式稳定，不需要通用 diff 解析器。
    """
    files: list[dict] = []
    cur: dict | None = None
    hunk: dict | None = None

    for raw in patch.split("\n"):
        if raw.startswith("diff --git "):
            cur = {"path": "", "added": 0, "removed": 0, "hunks": [],
                   "truncated": False}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if raw.startswith("+++ "):
            # "+++ b/frontend/app.js" → frontend/app.js；/dev/null 表示删除文件
            p = raw[4:].strip()
            cur["path"] = "（已删除）" if p == "/dev/null" else re.sub(r"^[ab]/", "", p)
            continue
        if raw.startswith("@@"):
            hunk = {"header": raw.strip(), "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            continue  # 文件头（index/---/mode 等）不展示
        if raw.startswith("+"):
            t, text = "add", raw[1:]
            cur["added"] += 1
        elif raw.startswith("-"):
            t, text = "del", raw[1:]
            cur["removed"] += 1
        elif raw.startswith(" ") or raw == "":
            t, text = "ctx", raw[1:] if raw else ""
        else:
            continue  # "\ No newline at end of file" 之类
        if sum(len(h["lines"]) for h in cur["hunks"]) >= MAX_DIFF_LINES_PER_FILE:
            cur["truncated"] = True
            continue
        hunk["lines"].append({"t": t, "text": text})

    return files


def show(ws: Path, commit_hash: str) -> dict:
    """单条提交的详情：作者/时间/完整信息 + 逐文件 diff。

    hash 由调用方校验格式（见 app.py 的 _GIT_HASH_RE）后再进来；这里只做
    长度兜底，不作为唯一防线。
    """
    meta_fmt = UNIT.join(["%H", "%h", "%an", "%ae", "%aI", "%B"])
    meta_out = _run(ws, ["show", "-s", f"--pretty=format:{meta_fmt}", commit_hash])
    parts = meta_out.split(UNIT, 5)
    if len(parts) < 6:
        raise GitError("无法解析该提交的信息")
    full, short, an, ae, date, body = parts
    me = identity(ws).get("email", "").lower()

    # --format= 让正文只输出 patch，不带提交头；-U3 是三行上下文的常规 diff。
    # 关键：merge 提交默认【不产出任何 patch】（git 无法替你在两条父链之间选
    # 一侧对比），于是详情页空白、用户以为"点了没反应"。用 --first-parent
    # 取"相对第一父提交"的差异——正是这个分支合并进来带来的改动，符合直觉。
    # 普通提交加这个选项无副作用。
    try:
        patch = _run(ws, ["show", "--format=", "--no-color", "-U3",
                          "--first-parent", commit_hash])
    except GitError:
        patch = ""
    files = _parse_patch(patch)

    # 是否为 merge 提交：%p 是父提交列表，两个以上 = merge
    try:
        parents = _run(ws, ["show", "-s", "--pretty=format:%p", commit_hash]).split()
    except GitError:
        parents = []
    is_merge = len(parents) > 1

    total = len(files)
    files = files[:MAX_FILES_PER_COMMIT]

    return {
        "hash": full, "short": short,
        "author": an, "email": ae, "date": date,
        "body": body.rstrip(),
        "mine": bool(me) and ae.lower() == me,
        "files": files,
        "file_count": total,
        "files_truncated": total > MAX_FILES_PER_COMMIT,
        "is_merge": is_merge,
        "parents": parents,
    }
