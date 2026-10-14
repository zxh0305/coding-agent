"""
持久记忆（索引/正文分离 + 轮末自动提取）
==========================================

参照 ZCode 的记忆设计：每条记忆一个 md 文件（frontmatter 三字段 + 正文一段话），
索引文件 MEMORY.md 一行一条。注入时【只注入索引】，模型判断需要正文时自己用
read_file 打开对应文件——索引常驻 system 的开销可控，正文按需读取不占上下文。

三个模块级职责：
1. 存储层：文件名/路径校验、frontmatter 解析渲染、索引行增删、原子写
   （.tmp + os.replace，读方永远看不到写了一半的文件）；
2. 注入层：system_memory_block 把「使用契约 + 当前索引」拼成 system 尾段；
3. 提取层：轮末自动提取（简化版：一发 JSON，宿主落盘，不做子代理）——
   输入 = 已有记忆清单 + 最近用户发言，LLM 返回 NOTHING_TO_SAVE 或操作清单，
   宿主执行器 apply_extraction 校验后落盘。

线程模型（与 app.py worker 的约定）：
  * 提取跑在 daemon 线程里，绝不阻塞回合收尾；每会话一把提取锁实现单飞，
    上一次未完成时本次直接跳过（提取不排队——攒着跑只会越积越多且过时）；
  * 提取全程静默：不发 SSE 事件、不写数据库。记忆是后台维护动作，用户不需要
    感知，推送事件反而会进环形缓冲、出现在所有订阅页的时间线上；失败只进日志，
    下轮自然重试。
  * 自动提取目前只挂在 Web worker（app.py _run_round 收尾处）；CLI 不触发
    （CLI 没有回合收尾时机，后续要挂的话调 run_extraction_async 即可，同一个钩子）。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("memory")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 记忆目录不再落在工作区（<ws>/.agent-memory/）：那会让没被 .gitignore 覆盖的
# 绑定目录出现 git 变动（用户实测踩过）。数据归数据——统一落本项目的数据区，
# 按工作区路径哈希分桶，不同项目互不串味；文件工具经沙箱的 .agent-memory/
# 前缀桥接读写（见 code_tools._resolve）。MEMORY_DIR_NAME 保留为桥接前缀名。
MEMORY_DIR_NAME = ".agent-memory"   # agent 视角的前缀名（桥接），也是历史遗留目录名
INDEX_NAME = "MEMORY.md"            # 索引文件：记忆目录内，无 frontmatter

# ---- 双层记忆的存储布局（记忆 v2，见 docs/memory-v2-design.md）----
# data/memory/projects/<project_id>/  项目记忆：这个代码库的事实
# data/memory/users/<user_id>/        个人记忆：关于用户这个人，跨项目
#
# 为什么要有个人层：旧布局按工作区路径哈希分桶（唯一的轴是 type），于是
# "用户偏好 pnpm 不用 npm" 这类【关于人】的偏好被存进了某个项目桶里，换个
# 目录打开就消失。根因是 type 一个枚举同时在回答两个正交问题——这条记忆
# 说谁的（who）和在哪成立（where）。owner/scope 两轴把它拆开。
PROJECTS_SUBDIR = "projects"
USERS_SUBDIR = "users"

# 项目身份的显式标记文件（三级解析的第 1 级，优先级最高）：
# 内容是一行 id，跟着目录走，改名/移动都不会丢记忆。
PROJECT_ID_MARKER = ".agent-memory-id"
# 标记值的白名单：字母数字开头，允许 . _ -，总长 <=64。
# 它会被直接当作目录名，所以必须挡住 / 与 ..（防路径逃逸）。
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# workspace 为空时的兜底记忆目录（数据区下不分桶）
_FALLBACK_MEM_DIR = Path(__file__).resolve().parent.parent / "data" / "memories" / "default"


def _legacy_mem_dir(workspace) -> Path | None:
    """2026-10-12 之前的旧布局：<workspace>/.agent-memory/。仅迁移用。"""
    if not workspace:
        return None
    return Path(workspace).expanduser() / MEMORY_DIR_NAME

# 记忆文件名白名单：小写字母/数字开头，只允许小写字母、数字、短横线。
# 正则天然拒绝路径分隔符与 ".."，是防路径逃逸的第一道闸。
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")

MEMORY_TYPES = ("user", "feedback", "project", "reference")

INDEX_MAX_LINES = 150     # 索引注入上限（行）：索引无限增长会挤占每次请求的 system
INDEX_MAX_CHARS = 20000   # 索引注入上限（字符）：单行超长描述的兜底
BODY_MAX_CHARS = 8000     # 单条记忆正文上限：提取模型偶尔长篇大论，截断保平安
INVENTORY_LIMIT = 200     # 提取请求带上的已有记忆清单条数上限（按 mtime 降序）。
                          # 曾为 50，而真实记忆已 74 条——清单里看不见的条目，
                          # 提取器就不知道它存在，"更新旧文件而不是新建重复"这条
                          # 纪律结构上无法执行，重复条目由此攒出来。上限只需
                          # 挡住无界增长，放到远高于真实规模即可。
SNAPSHOT_MESSAGES = 20    # 提取输入的快照窗口：最近 20 条消息里的用户发言
MIN_USER_WORDS = 3        # 短于 3 个词的用户消息（"好的"/"继续"）不值得提取

INDEX_HEADING = "用户记忆索引（跨会话持久）"

# 使用契约（原样注入 system）。写明类型语义、禁区、更新纪律，让模型在回合内
# 自己完成"写正文 + 更新索引"；轮末提取只是兜底，捕获模型没落盘的信息。
# 模型始终用 .agent-memory/ 前缀读写（沙箱桥接），存储位置的变化对模型透明。
MEMORY_CONTRACT = (
    "你有持久记忆目录 .agent-memory/，每条记忆是一个 md 文件。判断需要记住新信息时，"
    "直接用 write_file 写入，然后同步更新 MEMORY.md 索引加一行。类型语义："
    "user=用户身份与偏好；feedback=用户对工作方式的纠正与确认（必须带 Why 和 How to apply）；"
    "project=代码与 git 历史推导不出的进行中事项（相对日期转绝对日期）；"
    "reference=外部资源链接。禁止记录：代码结构、既往修复、git 可查的历史、仅本次对话"
    "有效的临时信息。写之前先看索引是否已有同类记忆，更新旧文件而不是新建重复文件；"
    "发现旧记忆与现状矛盾时修正或删除它。需要记忆正文时用 read_file 打开对应文件。"
)


# ---------------------------------------------------------------------------
# 路径与文件名
# ---------------------------------------------------------------------------

def workspace_token(workspace) -> str:
    """工作区 → 12 位十六进制 token（路径哈希）。同一路径恒定同一桶；
    路径移动/重命名视为新桶（记忆按位置分桶，不跟随目录搬家）。"""
    return hashlib.sha256(str(Path(workspace).expanduser().resolve()).encode("utf-8")
                          ).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 项目身份：让记忆跟着【项目】走，而不是跟着路径走
# ---------------------------------------------------------------------------
#
# 旧身份 = workspace_token（绝对路径哈希），有两个实测的硬伤：
#   1. 把项目目录改名/搬走 → 记忆当场失忆（哈希对不上，且没有任何东西把
#      新旧路径连起来）；对"用了六个月的个人记忆"这是不可接受的静默丢失；
#   2. 每个 scratch 目录、每个 worktree 都成为一个全新的失忆项目。
#
# 三级解析（前者优先）：
#   1. marker  —— <ws>/.agent-memory-id 里的一行 id。跟着目录走，最稳。
#   2. remote  —— git remote URL 归一后哈希。跨机器、跨 clone 稳定。
#   3. path    —— 回退到 workspace_token，与旧行为一致（保证老数据能对上）。
#
# 为什么不能只认 git remote（实测数据）：
#   * 34 个会话里 18 个 workspace=None —— 超过一半根本没有项目；
#   * 真实用过的 /Users/meta/Downloads、/Users/meta/Postman、/Volumes/agent_usb
#     都不是 git 仓库，remote 解析必然失败；
#   * mai_portal 有 2 个 remote（github + gitlab），只认 remote 会因"用哪个"
#     而再次分裂 —— 所以多 remote 时按名字排序取第一个，保证确定性。

def normalize_git_remote(url: str) -> str | None:
    """git remote URL → 稳定标识（去协议/去 .git/统一 scp 形式）。

    git@github.com:zxh0305/coding-agent.git  → github.com/zxh0305/coding-agent
    https://gitlab.metaglobal.cn/mai/x.git   → gitlab.metaglobal.cn/mai/x
    两种写法指向同一仓库时必须归一到同一个值，否则同一个项目在 ssh 与
    https 两种 clone 方式下会分裂成两个记忆桶。解析不出返回 None。
    """
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    # scp 形式 user@host:path → host/path（先于通用处理，否则冒号会被留下）
    m = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$", s)
    if m and "://" not in s:
        s = f"{m.group(1)}/{m.group(2)}"
    else:
        s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)   # 去 scheme
        s = re.sub(r"^[^@/]+@", "", s)                        # 去 user@
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    s = s.strip("/").lower()
    return s or None


def _git_remote_url(workspace) -> str | None:
    """取 workspace 的 remote URL。多 remote 时按名字排序取第一个（确定性）。

    复用 git_tools._run（已处理 no-shell / 超时 / 错误），失败一律返回 None
    —— 身份解析绝不能因为"git 没装"或"不是仓库"而抛异常影响主流程。
    局部 import 避免模块级循环依赖（git_tools 不 import memory）。
    """
    try:
        from git_tools import _run as _git_run, GitError
    except Exception:       # noqa: BLE001 —— 依赖不可用时静默降级到下一级
        return None
    ws = Path(workspace).expanduser()
    try:
        names = [n.strip() for n in _git_run(ws, ["remote"]).splitlines() if n.strip()]
        if not names:
            return None
        remote = "origin" if "origin" in names else sorted(names)[0]
        url = _git_run(ws, ["remote", "get-url", remote]).strip()
        return url or None
    except Exception:       # noqa: BLE001 —— GitError / OSError / 超时，全部降级
        return None


def _read_project_marker(workspace) -> str | None:
    """读显式标记文件。非法值（含 / 或 ..）返回 None 并告警——回退下一级。"""
    ws = Path(workspace).expanduser()
    for candidate in (ws / PROJECT_ID_MARKER, ws / ".git" / PROJECT_ID_MARKER):
        try:
            if not candidate.is_file():
                continue
            first = ""
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    first = line.strip()
                    break
            if not first:
                continue
            if PROJECT_ID_RE.match(first):
                return first
            log.warning("项目标记文件内容非法，已忽略：%s（值 %r）", candidate, first)
        except OSError as e:
            log.warning("项目标记文件读取失败：%s（%s）", candidate, e)
    return None


def project_identity(workspace) -> tuple[str | None, str]:
    """解析项目身份，返回 (project_id, source)。

    source ∈ {"marker", "git-remote", "path", "none"}，供 UI 提示
    "此项目按路径识别，改名会丢记忆"（只有 "path" 需要提示）。

    workspace 为空 → (None, "none")：无项目归属的会话（18/34 个），
    调用方据此跳过项目层，只注入个人层（Step 2 实现）。
    """
    if not workspace:
        return None, "none"
    marker = _read_project_marker(workspace)
    if marker:
        return marker, "marker"
    url = _git_remote_url(workspace)
    if url:
        normalized = normalize_git_remote(url)
        if normalized:
            return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12], "git-remote"
    return workspace_token(workspace), "path"


def _memory_root() -> Path:
    """记忆数据根 data/memory/。跟随 db.DB_PATH.parent（测试重定向即隔离）。"""
    import db  # 局部导入：db 不反向依赖本模块
    return db.DB_PATH.parent / "memory"


def memory_dir_for_project(workspace) -> Path:
    """项目记忆目录：data/memory/projects/<project_id>/。

    workspace 为空时退到 _FALLBACK_MEM_DIR（无项目归属 —— 这类会话在
    Step 2 只注入个人层）。只解析路径不创建，与 memory_dir 同一惰性契约。
    """
    if not workspace:
        return _FALLBACK_MEM_DIR
    pid, _source = project_identity(workspace)
    if not pid:
        return _FALLBACK_MEM_DIR
    return _memory_root() / PROJECTS_SUBDIR / pid


def memory_dir_for_user(user_id) -> Path:
    """个人记忆目录：data/memory/users/<user_id>/（一人一桶，永不随目录变）。

    user_id 为 None（未登录 / 无归属）时退到 _FALLBACK_MEM_DIR。
    """
    if user_id is None:
        return _FALLBACK_MEM_DIR
    return _memory_root() / USERS_SUBDIR / str(int(user_id))


def memory_dir(workspace=None) -> Path:
    """记忆目录（当前生效层）：**项目记忆** → data/memory/projects/<项目身份>/。

    这是 Step 1 之后唯一的项目记忆落点。历史：曾返回
    data/memory/<工作区token>/（纯绝对路径哈希）——那时注入/提取/桥接五处
    调用点都指向它。记忆 v2 迁移把内容搬进 projects/ 后，这里必须跟着切，
    否则注入会读到已空的旧桶、模型看不到任何记忆（迁移与切换不可分割）。

    workspace 为空时退到 _FALLBACK_MEM_DIR（无项目归属，Step 2 里这类会话
    只注入个人层）。只解析路径不创建——目录惰性创建（首次写入时），
    纯读路径（注入/清单扫描）绝不产生副作用。

    agent 读写记忆仍走文件工具，用 .agent-memory/ 相对前缀（沙箱桥接，
    见 code_tools._resolve），模型感知不到存储位置的变化。"""
    return memory_dir_for_project(workspace)


def valid_memory_filename(name) -> bool:
    """记忆文件名白名单校验。非法字符、../、绝对路径、非 .md 全在这里被拒。"""
    return bool(isinstance(name, str) and NAME_RE.fullmatch(name))


def _inside(root: str, target: Path) -> bool:
    """realpath 白名单：target 消解符号链接后必须仍在 root 之内（防逃逸第二道闸）。"""
    rp = os.path.realpath(target)
    return rp.startswith(root.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# frontmatter 与记忆文件（纯函数）
# ---------------------------------------------------------------------------

def render_frontmatter(name: str, description: str, mtype: str,
                       owner: str | None = None, scope: str | None = None,
                       updated: str | None = None) -> str:
    """frontmatter：三个必备字段 + 可选的新字段。description 压成单行——
    它要进索引行，换行会破坏"- 一行一条"的结构。

    新增字段全部可选且【默认不写】，因此现有调用方的产出逐字节不变
    （已知兼容性要求：老文件 parse 出来的仍是恰好三键）。
      owner   user | project —— 这条记忆说谁的（决定去哪一层）
      scope   global | repo  —— 在哪成立（仅 project 层有意义）
      updated ISO 日期       —— 判新旧；宿主盖章，不是模型自己写
    只写裸值，不写 YAML 行内注释——本模块的解析器极简，注释会被当成值的一部分。
    """
    description = " ".join(str(description).split())
    extra = ""
    if owner:
        extra += f"  owner: {owner}\n"
    if scope:
        extra += f"  scope: {scope}\n"
    if updated:
        extra += f"  updated: {updated}\n"
    return (f"---\nname: {name}\ndescription: {description}\nmetadata:\n"
            f"  type: {mtype}\n{extra}---\n")


def render_memory_file(name: str, description: str, mtype: str, body: str,
                       owner: str | None = None, scope: str | None = None,
                       updated: str | None = None) -> str:
    """完整的记忆文件内容 = frontmatter + 正文一段话。"""
    return (render_frontmatter(name, description, mtype, owner, scope, updated)
            + body.rstrip() + "\n")


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """解析 md 文件，返回 (frontmatter 三字段, 正文)。字段缺失/格式损坏一律返回
    (None, 原文)——三字段缺一不可，损坏文件不参与清单与注入，静默跳过。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    name = desc = mtype = None
    owner = scope = updated = None
    section = None  # 当前所在的顶层键（metadata 是唯一有缩进子键的）
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if line[:1] in (" ", "\t"):          # 缩进行：只认 metadata 下的子键
            if section == "metadata":
                key, _, value = stripped.partition(":")
                key, value = key.strip(), value.strip()
                if key == "type":
                    mtype = value or None
                elif key == "owner":
                    owner = value or None
                elif key == "scope":
                    scope = value or None
                elif key == "updated":
                    updated = value or None
        else:                                 # 顶层键
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()
            section = key
            if key == "name":
                name = value or None
            elif key == "description":
                desc = value or None
    meta = None
    if name and desc and mtype:
        # 必备三字段缺一不可（损坏文件静默跳过这一条不变）；
        # 新字段是可选增量：老文件解析出来仍是恰好三键，调用方按 .get 读。
        meta = {"name": name, "description": desc, "type": mtype}
        if owner:
            meta["owner"] = owner
        if scope:
            meta["scope"] = scope
        if updated:
            meta["updated"] = updated
    return meta, "\n".join(lines[end + 1:]).strip()


# ---------------------------------------------------------------------------
# 索引（MEMORY.md）：一行一条 `- [标题](文件.md) — 钩子`
# ---------------------------------------------------------------------------

def render_index_line(filename: str, description: str) -> str:
    """索引行：标题 = 去掉 .md 的 slug（稳定、唯一），钩子 = description
    （召回时判断相关性靠它，见 MEMORY_CONTRACT 的 description 语义）。"""
    hook = " ".join(str(description).split())
    return f"- [{filename[:-3]}]({filename}) — {hook}"


def upsert_index_line(index_text: str, filename: str, description: str) -> str:
    """更新/追加一条索引。按链接目标 `(文件名)` 识别旧行——模型手写的行文案
    可能各式各样，只认链接不认整行，才能既去重又把过时的钩子刷新。"""
    kept = [ln for ln in index_text.splitlines() if f"]({filename})" not in ln]
    kept.append(render_index_line(filename, description))
    return "\n".join(kept) + "\n"


def remove_index_line(index_text: str, filename: str) -> str:
    """删除某文件的索引行（delete 操作后同步清掉，防"正文没了、索引还挂着"）。"""
    kept = [ln for ln in index_text.splitlines() if f"]({filename})" not in ln]
    return "\n".join(kept) + "\n" if kept else ""


def truncate_index(index_text: str, max_lines: int = INDEX_MAX_LINES,
                   max_chars: int = INDEX_MAX_CHARS) -> str:
    """注入上限：150 行 / 20000 字符。磁盘上是全量真相，截断只发生在注入时刻；
    超限时在末尾加一行警告，让模型知道还有没看到的条目（想看就读文件）。"""
    lines = [ln for ln in index_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > max_chars:
            truncated = True
            break
        out.append(ln)
        used += len(ln) + 1
    if truncated:
        out.append(f"（警告：索引过长，仅加载了前 {len(out)} 条，完整索引请读 .agent-memory/{INDEX_NAME}）")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 原子写与读取
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, content: str) -> None:
    """先写同目录 .tmp 再 os.replace：replace 在同一文件系统内是原子的，
    并发读方（注入 / 模型 read_file）永远看不到写了一半的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)  # 记忆目录的惰性创建点
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def read_index_text(mem_dir: Path) -> str:
    """读索引原文。文件缺失/损坏/是目录……一律视为空——索引读失败绝不阻塞
    主循环，大不了这一轮没有记忆可注入。"""
    try:
        return (mem_dir / INDEX_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def scan_inventory(mem_dir: Path, limit: int = INVENTORY_LIMIT) -> list[dict]:
    """已有记忆清单（提取请求的输入①）：文件名 + type + description，
    按 mtime 降序最多 limit 条——最近动过的记忆最可能与当前对话相关。"""
    if not mem_dir.is_dir():
        return []
    entries = []
    try:
        for p in mem_dir.iterdir():
            if not p.name.endswith(".md") or p.name == INDEX_NAME or not p.is_file():
                continue
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except OSError:
        return []
    entries.sort(key=lambda e: e[0], reverse=True)
    items = []
    for _, p in entries[:limit]:
        meta, _ = parse_frontmatter(_read_text(p))
        items.append({"file": p.name,
                      "type": (meta or {}).get("type", ""),
                      "description": (meta or {}).get("description", "")})
    return items


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# 注入：契约 + 索引 → system 尾段
# ---------------------------------------------------------------------------

def memory_index_block(mem_dir: Path) -> str:
    """拼进 system 消息末尾的【动态索引段】（返回值以空行开头，调用方直接拼接）。

    使用契约不在这里——它已由 system_prompt.py 在 import 时拼进 SYSTEM_PROMPT
    末尾（import 而非副本，改契约常量两边自动同步）。Agent 的 _system_content
    因此只需补上这份动态索引，避免契约在 system 里出现两份；需要完整
    「契约 + 索引」段的调用方用 system_memory_block（单一来源组合，永不漂移）。

    关键不变式：记忆只进 system 消息，绝不进消息历史——上下文压缩只重写
    消息历史的模型视图、从不修改 system（见 agent.py 压缩注释），因此
    compact 后记忆原样保留，也不会被重复注入。
    索引每次组装时从磁盘现读：模型或提取线程刚写的记忆，下一轮立即可见；
    读失败视为空索引（read_index_text 兜底），不阻塞主循环。
    """
    index = truncate_index(read_index_text(mem_dir))
    index, withheld = redact_sensitive_lines(index)
    if withheld:
        index += f"\n（另有 {withheld} 条含敏感信息的记忆未注入，需用时请用户在本机确认）"
    body = index if index else "（暂无记忆）"
    return f"\n\n{INDEX_HEADING}\n\n{body}"


# ---------------------------------------------------------------------------
# 敏感信息闸门：注入前过滤（不是写入前过滤——写入闸门挡不住模型直接 write_file）
# ---------------------------------------------------------------------------
#
# 为什么必须在【注入】这一步拦：记忆索引每轮随 system 发往云端 LLM。一旦某条
# 记忆里出现服务器口令、token、私钥，它就每轮都出网一次，且"事后删文件"救不了
# ——泄露已经发生。真实事故：data/memory/<桶>/credentials-192.168.77.248.md
# 明文存着 SSH 与数据库口令，索引行里还直接写着目标 IP，等于向模型点名"这里有
# 这台机器的账号密码"。提取线程每轮还会把最近 20 条用户原话再发一次。
#
# 拦在注入层而不是提取层，是因为写入路径有两条（轮末提取 + 模型自己 write_file），
# 只在提取器里拦会漏掉后者；注入层是唯一的公共出口。命中即不注入并计数，
# 让模型知道"有条信息存在但不可见"，需要时请用户在本机确认——如实告知而非静默。
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd|passphrase)\b\s*[:=：]"),
    re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?key|token|bearer)\b\s*[:=：]"),
    re.compile(r"(?i)\b(private[_-]?key|id_rsa|BEGIN [A-Z ]*PRIVATE KEY)"),
    re.compile(r"ssh\s+\S+@|scp\s+\S+@"),               # ssh user@host
    re.compile(r"(?i)\b(mysql|psql|redis|mongodb|oracle)://\S+:\S+@"),  # 带口令的连接串
    # 中文关键词不能加 \b：CJK 是 word 字符而全角冒号不是，\b 在"口令："处不成立
    # （实测 '数据库口令：abc' 会漏），故用前后非中文字符的显式边界。
    re.compile(r"(?i)(账号|密码|口令|密钥|凭据|秘钥)\s*[:=：]"),
    re.compile(r"(?i)(数据库|服务器|主机|登录)\s*\S{0,10}(账号|密码|口令)"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b.{0,20}\b(密码|口令|账号|password)\b"),
    re.compile(r"(?i)\b[A-Za-z0-9+/]{40,}={0,2}\b"),     # 长 base64 串（密钥/令牌）
]


def is_sensitive_text(text: str) -> bool:
    """文本是否含疑似凭据/密钥。宁可多拦一条（模型看不到，可请用户确认），
    也不要漏一条（每轮出网、不可撤回）。"""
    if not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


def redact_sensitive_lines(index_text: str) -> tuple[str, int]:
    """从索引里滤掉含敏感信息的行，返回 (清洗后的索引, 被滤条数)。

    逐行判敏感，不只看 "- " 开头的条目行：索引文件可能被模型直接改写、混进
    非条目行（实测过裸的 `password: hunter2` 这种情况会被漏放），任何一行
    命中都拦。被滤的行不出现在注入内容里——但计数会告诉模型"有 N 条不可见"，
    而不是让它以为记忆里没有这件事（静默丢失比看不到更糟）。
    """
    kept, withheld = [], 0
    for line in (index_text or "").splitlines():
        if line.strip() and is_sensitive_text(line):
            withheld += 1
            continue
        kept.append(line)
    return "\n".join(kept), withheld


def system_memory_block(mem_dir: Path) -> str:
    """完整记忆段 = 使用契约 + 索引（对两者做单一来源的组合，两段永不漂移）。

    Agent 的新链路只用 memory_index_block（契约已内置于 SYSTEM_PROMPT 末尾）；
    本函数保留为兼容入口。"""
    return f"\n\n{MEMORY_CONTRACT}" + memory_index_block(mem_dir)


# ---------------------------------------------------------------------------
# 轮末自动提取：输入构造（纯函数）
# ---------------------------------------------------------------------------

# 中文没有空格分词，按 \w+ 数"词"会把整句中文算成 1 个词、全被滤掉；
# 折中：每个 CJK 字符计 1 词、连续拉丁/数字串计 1 词，够区分"好的"和正经发言。
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def message_text(content) -> str:
    """OpenAI content 兼容取正文：字符串原样；数组只拼 text 部分（图片跳过）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def recent_user_texts(history: list[dict], limit: int = SNAPSHOT_MESSAGES,
                      min_words: int = MIN_USER_WORDS) -> list[str]:
    """提取请求的输入②：快照最近 limit 条消息里 role=user 的原文，滤掉
    过短消息与 _synthetic 合成消息（收尾指令/循环提醒是运行时构造的，不是
    用户说的话，不该被提炼成记忆）。过滤发生在快照上而非活历史上——调用方
    （worker）在启动提取线程前把本函数的返回值（纯字符串列表）快照出来，
    线程内不碰任何可变状态。
    """
    texts = []
    for m in history[-limit:]:
        if m.get("role") != "user" or m.get("_synthetic"):
            continue
        text = message_text(m.get("content")).strip()
        if text and len(_WORD_RE.findall(text)) >= min_words:
            texts.append(text)
    return texts


EXTRACT_SYSTEM_PROMPT = """\
你是持久记忆的提取器。分析用户的最近发言，判断是否有值得写入记忆目录的新信息\
（user=身份与偏好 / feedback=对工作方式的反馈 / project=代码与 git 推导不出的\
进行中事项 / reference=外部资源）。禁区：代码结构、既往修复、git 可查的历史、\
仅本次对话有效的临时信息。

输出规则（二选一，绝不输出任何其他内容）：
1. 没有值得记住的新信息 → 只输出 NOTHING_TO_SAVE；
2. 有 → 只输出一个 JSON 对象（不要包代码块、不要解释）：
{"memories": [{"action": "write", "file": "slug-name.md", "frontmatter": \
{"name": "slug-name", "description": "一句话摘要", "metadata": {"type": "user"}}, \
"body": "正文一段话"}]}
要求：
- 要更新已有记忆清单里的某条，就用 action="write" + 沿用它的文件名（body 给更新后的\
完整正文）——写入即覆盖，不需要独立的 update 动作；已确认过时的记忆才用 action="delete"；\
- 没有任何值得记住的新信息就返回 NOTHING_TO_SAVE；
- action 只能是 "write" 或 "delete"，其它取值会让整条被拒；
- file 只能是小写字母/数字/短横线 + .md；
- feedback 类型的 body 必须包含 **Why:** 与 **How to apply:** 两行；
- 相关记忆之间用 [[name]] 互相链接（指向尚不存在的记忆也合法）；相对日期改成绝对日期。\
"""


def build_extraction_messages(inventory: list[dict], user_texts: list[str]) -> list[dict]:
    """提取调用的完整消息体（纯函数）：清单 + 用户发言 → [system, user]。"""
    inv = "\n".join(f"- {it['file']}（type={it['type'] or '未知'}）{it['description']}"
                    for it in inventory) or "（暂无记忆）"
    texts = "\n".join(f"{i}. {t}" for i, t in enumerate(user_texts, 1)) or "（无）"
    content = (f"=== 已有记忆清单（要更新某条就用 action=write + 同名 file 覆盖） ===\n{inv}\n\n"
               f"=== 最近用户发言 ===\n{texts}")
    return [{"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": content}]


def parse_extraction_reply(text: str) -> list | None:
    """模型回复 → 操作清单（纯函数）。NOTHING_TO_SAVE → []；解析失败 → None
    （调用方静默放弃本次，下轮自然重试）。容忍 markdown 代码块围栏与前后寒暄：
    取第一个 { 到最后一个 } 之间的内容解析。"""
    if not text or not text.strip():
        return None
    stripped = text.strip()
    if "NOTHING_TO_SAVE" in stripped:
        return []
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("memories"), list):
        return data["memories"]
    return None


# ---------------------------------------------------------------------------
# 宿主执行器：校验 + 落盘（只碰文件系统，纯函数可直接单测）
# ---------------------------------------------------------------------------

def _validate_op(op, mem_dir: Path) -> str | None:
    """校验一条操作，非法返回原因字符串。文件名走正则白名单（第一节），
    realpath 再兜一层防逃逸——双保险都在写盘【之前】完成。"""
    if not isinstance(op, dict):
        return "操作不是对象"
    action = op.get("action")
    if action not in ("write", "delete"):
        return f"非法 action: {action!r}"
    fname = op.get("file")
    if not valid_memory_filename(fname):
        return f"非法文件名: {fname!r}"
    if not _inside(os.path.realpath(str(mem_dir)), mem_dir / fname):
        return f"路径逃逸: {fname!r}"
    if action == "write":
        fm = op.get("frontmatter")
        if not isinstance(fm, dict):
            return "frontmatter 缺失"
        desc = fm.get("description")
        if not isinstance(desc, str) or not desc.strip():
            return "description 缺失或为空"
        meta = fm.get("metadata")
        mtype = meta.get("type") if isinstance(meta, dict) else None
        if mtype not in MEMORY_TYPES:
            return f"非法 type: {mtype!r}"
        # name 以 file 字段为准；frontmatter.name 缺省归一为文件名主干，
        # 给了但不匹配也不整批报废（归一，避免无意义的重试循环）
        name = fm.get("name", Path(fname).stem)
        if not valid_memory_filename(f"{name}.md"):
            return f"非法 name: {name!r}"
        if not isinstance(op.get("body"), str):
            return "body 缺失或不是字符串"
    return None


def apply_extraction(mem_dir: Path, payload) -> dict:
    """宿主执行器：把提取模型产出的操作清单落盘。

    输入：记忆目录 + 清单 JSON（{"memories": [...]}）；输出：执行结果
    {"written": [...], "deleted": [...], "rejected": [(file, 原因)], "abandoned": 原因|None}。
    只碰记忆目录内的文件，无网络、无全局状态——纯函数，单测直接喂 JSON。

    【逐条落盘，一条坏操作只丢它自己】。此前是"全有或全无"——任一条非法就
    放弃整批、磁盘一个字节不动。配合提示词里"优先用 action=update"（而校验器
    只认 write/delete），后果是：模型越守规矩地更新旧记忆，整批越会被整批丢弃
    ——连同同批完全合法的 write 一起。表现为"刚讨论出的重要结论下次完全不记得"，
    且用户毫无感知，下一轮照旧重试、照旧丢。
    现在每条独立校验、独立落盘；被拒的记进 rejected（带原因）供日志排查，
    合法的照常生效。半套记忆的风险由"单条原子写"和索引随写同步兜住
    （write 先写正文再更新索引，delete 先删正文再删索引行，两步各自原子）。
    """
    result = {"written": [], "deleted": [], "rejected": [], "abandoned": None}
    ops = payload.get("memories") if isinstance(payload, dict) else None
    if not isinstance(ops, list):
        result["abandoned"] = "清单不是 {'memories': [...]} 形状"
        return result
    for op in ops:
        reason = _validate_op(op, mem_dir)
        if reason:
            # 单条被拒：不影响同批其它条目。file 可能不是合法文件名，取原始值兜底。
            fname = op.get("file") if isinstance(op, dict) else None
            result["rejected"].append((str(fname), reason))
            log.warning("记忆操作被拒（%s）：%s", fname, reason)
            continue
        fname, target = op["file"], mem_dir / op["file"]
        try:
            if op["action"] == "write":
                fm = op["frontmatter"]
                body = op["body"]
                if len(body) > BODY_MAX_CHARS:  # 提取模型偶尔长篇大论，截断保平安
                    body = body[:BODY_MAX_CHARS]
                atomic_write_text(target, render_memory_file(
                    Path(fname).stem, fm["description"], fm["metadata"]["type"], body))
                # 提取是宿主行为，模型没机会改索引：索引行由执行器代为同步——
                # 否则正文落了盘、索引没更新，注入永远看不到这条记忆。
                atomic_write_text(mem_dir / INDEX_NAME,
                                  upsert_index_line(read_index_text(mem_dir),
                                                    fname, fm["description"]))
                result["written"].append(fname)
            else:
                existed = target.exists()
                target.unlink(missing_ok=True)  # 已不存在视为删除成功（幂等）
                atomic_write_text(mem_dir / INDEX_NAME,
                                  remove_index_line(read_index_text(mem_dir), fname))
                if existed:
                    result["deleted"].append(fname)
        except OSError as e:
            # 单条落盘失败（磁盘满、权限、索引写不进去）只丢这一条：
            # 异常不能冒泡到调用方，否则整批的成果随一次磁盘抖动一起丢。
            result["rejected"].append((fname, f"{type(e).__name__}: {e}"))
            log.warning("记忆落盘失败（%s）：%s", fname, e)
            continue
    return result


# ---------------------------------------------------------------------------
# 单飞锁与提取入口
# ---------------------------------------------------------------------------

# 每会话一把提取锁：dict[session_id, Lock]。数量级 = 会话数，常驻可接受
# （与 app.py 的 _session_locks 同一模式）。
_extraction_locks: dict[str, threading.Lock] = {}
_extraction_locks_guard = threading.Lock()


def _extraction_lock(sid: str) -> threading.Lock:
    with _extraction_locks_guard:
        return _extraction_locks.setdefault(sid, threading.Lock())


def drop_extraction_lock(sid: str) -> None:
    """会话删除时回收该会话的提取锁。

    原注释假设"数量级 = 会话数，常驻可接受"——只在会话从不被删除时成立；
    长期增删会让锁表随删除过的会话数无界增长（与 app.py 的 _session_locks
    同类问题）。删会话时必须显式回收，否则慢性泄漏。

    安全性：提取是轮末异步单飞，会话删除时该会话已无新回合；即便某次提取
    正持锁，pop 掉也只是让下次（不会发生的）调用拿到新锁，旧锁随线程回收。
    """
    with _extraction_locks_guard:
        _extraction_locks.pop(sid, None)


def extract_memories(sid: str, mem_dir: Path, chat, user_texts: list[str]) -> dict:
    """同步执行一次轮末提取。绝不抛异常、不发事件、不写数据库，失败只进日志。

    chat：fn(messages, temperature=0) -> message dict（该会话的 llm_client）。
    user_texts：调用方快照好的用户发言（纯字符串），线程内只用快照。

    单飞：acquire(blocking=False) 拿不到锁 = 上一次还没跑完 → 直接跳过。
    """
    lock = _extraction_lock(sid)
    if not lock.acquire(blocking=False):
        log.info("[会话 %s] 记忆提取：上一次未完成，本次跳过（单飞）", sid)
        return {"skipped": True}
    try:
        if not user_texts:
            # 没有可分析的用户发言（全是"好的/继续"），连 LLM 调用都省掉
            return {"skipped": False, "written": [], "deleted": []}
        messages = build_extraction_messages(scan_inventory(mem_dir), user_texts)
        reply = chat(messages, temperature=0)  # temperature=0：提取要确定性，不要发挥
        ops = parse_extraction_reply((reply or {}).get("content") or "")
        if ops is None:
            log.warning("[会话 %s] 记忆提取：模型输出无法解析，本次放弃（下轮重试）", sid)
            return {"abandoned": "模型输出无法解析"}
        if not ops:  # NOTHING_TO_SAVE
            return {"written": [], "deleted": []}
        outcome = apply_extraction(mem_dir, {"memories": ops})
        if outcome.get("abandoned"):
            log.warning("[会话 %s] 记忆提取：操作非法已放弃（%s，下轮重试）",
                        sid, outcome["abandoned"])
        else:
            log.info("[会话 %s] 记忆提取完成：写入 %s，删除 %s",
                     sid, outcome["written"], outcome["deleted"])
        return outcome
    except Exception:
        # 提取是锦上添花的后台维护：任何失败（网络/盘/解析）都不允许影响
        # 回合与下一轮，记日志了事，下轮自然重试
        log.exception("[会话 %s] 记忆提取失败（静默忽略）", sid)
        return {"error": True}
    finally:
        lock.release()


def run_extraction_async(sid: str, mem_dir: Path, chat, user_texts: list[str]) -> None:
    """worker 调用的入口：daemon 线程跑 extract_memories，绝不阻塞回合收尾。

    入参全部是不可变快照（字符串列表 / 路径 / 客户端引用），提取线程不持有
    会话锁（worker 要拿锁跑下一回合）、不与 worker 共享任何可变状态。
    """
    threading.Thread(target=extract_memories, daemon=True,
                     args=(sid, mem_dir, chat, list(user_texts)),
                     name=f"memory-extract-{sid}").start()


# ---------------------------------------------------------------------------
# 记忆 v2 布局迁移（一次性；由 db.init_db 的迁移 v26 调用）
# ---------------------------------------------------------------------------
#
# 旧布局：data/memory/<工作区路径哈希>/        扁平、按路径分桶
# 新布局：data/memory/projects/<project_id>/   项目身份（marker→remote→path）
#         data/memory/users/<user_id>/         个人层（本步只建空桶）
#
# 三条硬性质：
#   1. 幂等——由 db 的 PRAGMA user_version 记账（跑过就不再跑），且函数自身
#      对"目标已存在"再兜一层：projects/ 已存在则连备份都跳过。
#   2. 先备份、备份失败则【一个文件都不动】——半个备份比不迁移更糟。
#   3. 逐桶容错——某个桶出 OSError 只跳过它并告警，绝不整批放弃
#      （记忆全有或全无的教训刚在阶段 0 修过）。
#
# 本步【不写 owner 字段】：74+7 条原样留作 project，用户日后在 UI 里手动提升。
# 误判成 user 会污染所有项目上下文，且是静默的不可逆方向，所以不由迁移代劳。

def _merge_index_text(base_text: str, incoming_text: str) -> str:
    """合并两份索引：按 `](文件名)` 键去重，base 优先（同名保留已有那行）。

    与 upsert_index_line/remove_index_line 同一套"只认链接目标"的键语义，
    保证合并结果与后续增删的判定口径一致。
    """
    def key_of(line: str) -> str | None:
        m = re.search(r"\]\(([^)]+)\)", line)
        return m.group(1) if m else None

    lines: list[str] = []
    seen: set[str] = set()
    for text in (base_text, incoming_text):
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            k = key_of(line)
            if k is None:
                continue
            if k in seen:
                continue
            seen.add(k)
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def migrate_memory_layout() -> dict:
    """把旧扁平布局搬进 projects/<project_id>/，并建 users/<uid>/ 空桶。

    返回对账 dict（供日志/启动打印）：
      {skipped, reason, moved_dirs, moved_files, kept, collisions, backups, users, projects}
    绝不抛异常——调用方是 init_db，记忆迁移失败不该阻止服务启动。
    """
    import db  # 局部导入（db 不反向依赖本模块，只借 DB_PATH 与查询）

    result = {"skipped": False, "reason": "", "moved_dirs": 0, "moved_files": 0,
              "kept": [], "collisions": [], "backup": None, "users": 0,
              "projects": []}

    root = _memory_root()
    if not root.is_dir():
        result["skipped"] = True
        result["reason"] = "无记忆数据区（全新安装或测试环境）"
        return result
    if (root / PROJECTS_SUBDIR).is_dir():
        # 迁移过的标记：第二次启动不该再备份/再搬
        result["skipped"] = True
        result["reason"] = "已是新布局（projects/ 已存在）"
        return result

    # ---- 1. 备份：失败就整体放弃，一个文件都不动 ----
    backup = root.parent / "backups" / f"memory-pre-v2-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, backup / "memory", dirs_exist_ok=True)
    except OSError as e:
        log.error("记忆迁移：备份失败，已放弃迁移（数据未改动）：%s", e)
        result["skipped"] = True
        result["reason"] = f"备份失败：{e}"
        return result
    result["backup"] = str(backup)

    # ---- 2. 逐工作区搬迁（身份一律现解析，绝不 iterdir 盲扫）----
    old_tokens: set[str] = set()
    for ws in db._all_session_workspaces():
        try:
            token = workspace_token(ws)
            src = root / token
            if not src.is_dir():
                continue          # 该工作区没有记忆（大多数是这种情况）
            pid, source = project_identity(ws)
            if not pid:
                continue
            dst = root / PROJECTS_SUBDIR / pid
            dst.mkdir(parents=True, exist_ok=True)
            moved_here = 0
            for p in sorted(src.iterdir()):
                if not p.is_file() or not p.name.endswith(".md"):
                    continue
                target = dst / p.name
                if p.name == INDEX_NAME:
                    # 索引：合并去重，绝不覆盖（可能是两个旧桶合流到同一项目）
                    merged = _merge_index_text(
                        read_index_text(dst), read_index_text(src))
                    atomic_write_text(target, merged)
                    continue
                if target.exists():
                    # 同名冲突：保留已有那份，源文件留在原地不删（保守），告警
                    result["collisions"].append(f"{ws}:{p.name}")
                    log.warning("记忆迁移：%s 已存在于目标项目，保留原有那份；"
                                "源文件留在 %s", p.name, src)
                    continue
                try:
                    shutil.move(str(p), str(target))
                    moved_here += 1
                    result["moved_files"] += 1
                except OSError as e:
                    log.warning("记忆迁移：移动失败 %s（跳过，继续其它文件）：%s", p, e)
            # 迁移完成后旧桶应只剩已被合并走的 MEMORY.md：内容已在 projects/ 里，
            # 旧索引留着只会造成"两处都有记忆"的困惑。先删掉它再尝试删目录。
            # 有冲突文件（保留了源文件）时不删，留待人工处理并告警。
            has_conflict = any(
                p.name != INDEX_NAME and p.is_file()
                for p in src.iterdir()) if src.is_dir() else False
            if not has_conflict and moved_here:
                try:
                    (src / INDEX_NAME).unlink(missing_ok=True)
                except OSError as e:
                    log.warning("记忆迁移：清理旧索引失败 %s：%s", src / INDEX_NAME, e)
            if src.is_dir() and not has_conflict:
                try:
                    src.rmdir()       # 只在空了才删得掉；非空留着并告警
                    result["moved_dirs"] += 1
                except OSError:
                    result["kept"].append(str(src))
            old_tokens.add(token)
            result["projects"].append({"workspace": ws, "project_id": pid,
                                       "source": source})
        except Exception as e:        # noqa: BLE001 —— 单桶失败不影响其它桶
            log.warning("记忆迁移：工作区 %s 处理失败（跳过）：%s", ws, e)

    # ---- 3. 个人层空桶（本步不写入任何记忆，只建目录占位）----
    for uid in db.all_user_ids():
        try:
            (root / USERS_SUBDIR / str(uid)).mkdir(parents=True, exist_ok=True)
            result["users"] += 1
        except OSError as e:
            log.warning("记忆迁移：个人层建桶失败 uid=%s：%s", uid, e)

    log.info("记忆迁移完成：搬移 %d 个项目目录 / %d 个文件，个人层 %d 个空桶，"
             "备份 %s", result["moved_dirs"], result["moved_files"],
             result["users"], result["backup"])
    return result
