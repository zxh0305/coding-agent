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

import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("memory")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MEMORY_DIR_NAME = ".agent-memory"   # 记忆目录：固定在工作区内，复用工具沙箱
INDEX_NAME = "MEMORY.md"            # 索引文件：记忆目录内，无 frontmatter

# workspace 为空时的兜底记忆目录（backend 同级的 data/memories/default/）
_FALLBACK_MEM_DIR = Path(__file__).resolve().parent.parent / "data" / "memories" / "default"

# 记忆文件名白名单：小写字母/数字开头，只允许小写字母、数字、短横线。
# 正则天然拒绝路径分隔符与 ".."，是防路径逃逸的第一道闸。
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")

MEMORY_TYPES = ("user", "feedback", "project", "reference")

INDEX_MAX_LINES = 150     # 索引注入上限（行）：索引无限增长会挤占每次请求的 system
INDEX_MAX_CHARS = 20000   # 索引注入上限（字符）：单行超长描述的兜底
BODY_MAX_CHARS = 8000     # 单条记忆正文上限：提取模型偶尔长篇大论，截断保平安
INVENTORY_LIMIT = 50      # 提取请求带上的已有记忆清单条数上限（按 mtime 降序）
SNAPSHOT_MESSAGES = 20    # 提取输入的快照窗口：最近 20 条消息里的用户发言
MIN_USER_WORDS = 3        # 短于 3 个词的用户消息（"好的"/"继续"）不值得提取

INDEX_HEADING = "用户记忆索引（跨会话持久）"

# 使用契约（原样注入 system）。写明类型语义、禁区、更新纪律，让模型在回合内
# 自己完成"写正文 + 更新索引"；轮末提取只是兜底，捕获模型没落盘的信息。
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

def memory_dir(workspace=None) -> Path:
    """记忆目录：<workspace>/.agent-memory/。workspace 为空时退到
    data/memories/default/。只解析路径不创建——目录惰性创建（首次写入时），
    纯读路径（注入/清单扫描）绝不产生副作用。"""
    if workspace:
        return Path(workspace) / MEMORY_DIR_NAME
    return _FALLBACK_MEM_DIR


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

def render_frontmatter(name: str, description: str, mtype: str) -> str:
    """frontmatter 只含三个字段。description 压成单行——它要进索引行，
    换行会破坏"- 一行一条"的结构。"""
    description = " ".join(str(description).split())
    return f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {mtype}\n---\n"


def render_memory_file(name: str, description: str, mtype: str, body: str) -> str:
    """完整的记忆文件内容 = frontmatter + 正文一段话。"""
    return render_frontmatter(name, description, mtype) + body.rstrip() + "\n"


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
    section = None  # 当前所在的顶层键（metadata 是唯一有缩进子键的）
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if line[:1] in (" ", "\t"):          # 缩进行：只认 metadata 下的 type
            if section == "metadata":
                key, _, value = stripped.partition(":")
                if key.strip() == "type":
                    mtype = value.strip() or None
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
        meta = {"name": name, "description": desc, "type": mtype}
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

def system_memory_block(mem_dir: Path) -> str:
    """拼进 system 消息末尾的记忆段（返回值以空行开头，调用方直接拼接）。

    关键不变式：记忆只进 system 消息，绝不进消息历史——上下文压缩只重写
    消息历史的模型视图、从不修改 system（见 agent.py 压缩注释），因此
    compact 后记忆原样保留，也不会被重复注入。
    索引每次组装时从磁盘现读：模型或提取线程刚写的记忆，下一轮立即可见；
    读失败视为空索引（read_index_text 兜底），不阻塞主循环。
    """
    index = truncate_index(read_index_text(mem_dir))
    body = index if index else "（暂无记忆）"
    return f"\n\n{MEMORY_CONTRACT}\n\n{INDEX_HEADING}\n\n{body}"


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
- 优先 update 已有记忆清单里的文件（file 沿用其文件名、body 给出更新后的完整正文），\
无新信息就返回 NOTHING_TO_SAVE；确认某条记忆已过时才用 action=delete；
- file 只能是小写字母/数字/短横线 + .md；
- feedback 类型的 body 必须包含 **Why:** 与 **How to apply:** 两行；
- 相关记忆之间用 [[name]] 互相链接（指向尚不存在的记忆也合法）；相对日期改成绝对日期。\
"""


def build_extraction_messages(inventory: list[dict], user_texts: list[str]) -> list[dict]:
    """提取调用的完整消息体（纯函数）：清单 + 用户发言 → [system, user]。"""
    inv = "\n".join(f"- {it['file']}（type={it['type'] or '未知'}）{it['description']}"
                    for it in inventory) or "（暂无记忆）"
    texts = "\n".join(f"{i}. {t}" for i, t in enumerate(user_texts, 1)) or "（无）"
    content = (f"=== 已有记忆清单（优先 update 这些文件） ===\n{inv}\n\n"
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
    {"written": [...], "deleted": [...], "abandoned": 原因|None}。
    只碰记忆目录内的文件，无网络、无全局状态——纯函数，单测直接喂 JSON。

    全有或全无：任何一条操作非法就放弃整批（磁盘一个字节不动）——半套记忆
    比没有更糟（比如正文删了、索引行还挂着），放弃后下轮提取自然重试。
    """
    result = {"written": [], "deleted": [], "abandoned": None}
    ops = payload.get("memories") if isinstance(payload, dict) else None
    if not isinstance(ops, list):
        result["abandoned"] = "清单不是 {'memories': [...]} 形状"
        return result
    for op in ops:  # 第一遍：全部校验通过才动手
        reason = _validate_op(op, mem_dir)
        if reason:
            result["abandoned"] = reason
            return result
    for op in ops:  # 第二遍：落盘
        fname, target = op["file"], mem_dir / op["file"]
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
