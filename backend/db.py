"""
数据持久层（SQLite，标准库 sqlite3，零依赖）
=============================================

早期版本把对话存在服务进程的内存 dict 里，重启即丢；现在统一落盘到项目根目录的
agent_data.db（SQLite 单文件数据库，可直接用任何 SQLite 工具打开查看）：

  users            登录用户：用户名 / 密码哈希（PBKDF2，不存明文）
  auth_tokens      登录令牌：随机 token -> 用户，重启不失效
  sessions         任务（会话）：标题、创建/更新时间、归属用户、各自的工作区
  messages         消息历史：稳定身份 mid + 显示序 ord + 消息正文（增量落盘，
                   超大正文外置到 artifacts/ 文件，行内只留 head/tail 摘要）
  message_usage    消息级统计（token 用量等）：与 messages.content 分离存储
  providers        模型供应商：名称 / Base URL / API 格式 / API Key / 启用状态 / 默认窗口
  provider_models  供应商下的模型：模型名 / 上下文窗口 / 启用 / 是否支持视觉
  settings         键值设置（当前激活的模型等）

消息存储的三条设计原则（对写放大 / 行大小 / 恢复内存三者的处理）：
  1. 稳定身份 + 增量落盘：每条消息有跨轮不变的 mid（uuid4），save_messages 按
     「内容指纹」跳过已落盘的消息，每轮只写新增——不再是 DELETE 全表重写；
  2. 大内容外置：单条序列化超过 MAX_INLINE_BYTES 的消息，正文写进
     artifacts/<session_id>/<mid>.json 文件，行内只留摘要（head/tail），
     单行大小有上限，长会话不会把库文件撑出巨型行；
  3. 按窗口加载：get_messages 支持 since_ord/before_ord/limit，会话恢复只把
     压缩边界（及锚点消息）之后的消息读进内存，内存占用与当前窗口成正比。

schema 演进用 PRAGMA user_version + 有序迁移列表 MIGRATIONS 管理（见 init_db），
替代早期散落各处的幂等 ALTER TABLE。

首库自动播种：providers 表为空时，把 .env 里的 LLM_* 配置导入为"默认"供应商。
关于 API Key：以明文存在本机数据库里（学习项目的务实选择），接口回显一律打码。
用户体系：登录只做身份区分与会话隔离（任务列表按用户过滤），供应商/模型
仍是全局共享——所有登录用户共用服务端配置的 LLM Key；工作区按任务隔离
（每个任务可有自己的工作区，切换互不影响）。
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

log = logging.getLogger("db")

DB_PATH = Path(__file__).resolve().parent.parent / "agent_data.db"

# ---------------------------------------------------------------------------
# 存储常量
# ---------------------------------------------------------------------------

# 单条消息允许内联进 messages.content 的最大字节数。超过它就把完整 JSON 外置到
# artifacts 文件、行内只留 head/tail 摘要。64KB 的量级依据：SQLite 单页默认 4KB，
# 64KB 的行要跨 16+ 页读写，且"每轮重写整表"时代最大的性能杀手正是这种巨型行；
# 而正常一轮工具输出 / 回答远小于它，几乎不会触发外置。
MAX_INLINE_BYTES = 65536

# ord 的相邻间隔。ord 是"显示顺序"而不是物理行号：压缩会往历史中段插入边界
# 消息，若 ord 连续编号（0,1,2,…），插入点之后的每条消息都得全体 +1 重写，
# 增量落盘就名存实亡。预留间隔后，边界只需取两侧 ord 的中点即可获得唯一且
# 单调的序号——已有消息一个都不用动。（间隔会被中点操作逐渐吃掉，但压缩只
# 会插在"上一条边界之后"的活区里，同一对消息之间至多插入一次，实际耗不尽。）
ORD_GAP = 1024


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL：写不阻塞读。每轮回答结束会把新增消息增量落盘（save_messages），
    # 默认 journal 模式下这期间其他请求的读写都要排队；WAL 让它们互不等待。
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL 推荐档位：断电最多丢最后一次写入，库文件本身不会损坏
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# schema 迁移（PRAGMA user_version + 有序迁移列表）
# ---------------------------------------------------------------------------
#
# 早期版本的升级方式是 init_db 里散落一堆 try/except 幂等 ALTER，缺点是：
# 无版本号、无顺序保证、每条升级都要靠"报错就跳过"来兜底。现在收编为有序
# 迁移列表——启动时读 PRAGMA user_version，只执行编号大于它的项，执行完写回
# 最大编号。幂等性双保险：user_version 记账之外，每条迁移还有"已应用探针"
# （列是否存在），即便版本号丢失（比如手工拷过库文件）重放也不会坏数据。

# 基础 DDL：全新库直接建最终形态（老库已存在的表 IF NOT EXISTS 全部跳过，
# 由迁移负责把老表搬到新形态）。注意 messages 的 (session_id, ord) 索引不在这里：
# 老库的 messages 还没有 ord 列，建索引会直接报错——索引归迁移 6 管。
_BASE_DDL = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created REAL
);
CREATE TABLE IF NOT EXISTS auth_tokens(
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created REAL
);
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    user_id INTEGER,
    created REAL,
    updated REAL,
    workspace TEXT
);
CREATE TABLE IF NOT EXISTS messages(
    mid TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(session_id, mid)
);
CREATE TABLE IF NOT EXISTS providers(
    id TEXT PRIMARY KEY,
    name TEXT,
    base_url TEXT,
    api_format TEXT DEFAULT 'openai',
    api_key TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    context_window INTEGER DEFAULT 128000,
    created REAL
);
CREATE TABLE IF NOT EXISTS provider_models(
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    context_window INTEGER DEFAULT 262144,
    enabled INTEGER DEFAULT 1,
    vision INTEGER DEFAULT 0,
    PRIMARY KEY (provider_id, name)
);
CREATE TABLE IF NOT EXISTS message_usage(
    session_id TEXT NOT NULL,
    mid TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    stats_json TEXT,
    PRIMARY KEY(session_id, mid)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

# messages 表的最终形态（迁移 6 的建新表语句，与 _BASE_DDL 保持一致）
_MESSAGES_DDL = """
CREATE TABLE messages(
    mid TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(session_id, mid)
)
"""

# 有序迁移列表：(版本号, SQL)。SQL 为 None 表示该步是过程式迁移（见 _MIGRATION_FN）。
MIGRATIONS: list[tuple[int, str | None]] = [
    (1, "ALTER TABLE providers ADD COLUMN api_format TEXT DEFAULT 'openai'"),
    (2, "ALTER TABLE sessions ADD COLUMN user_id INTEGER"),
    (3, "ALTER TABLE provider_models ADD COLUMN vision INTEGER DEFAULT 0"),
    (4, "ALTER TABLE sessions ADD COLUMN workspace TEXT"),
    (5, "ALTER TABLE providers ADD COLUMN context_window INTEGER DEFAULT 128000"),
    # 6：messages 表重建（seq/idx 旧主键 → mid/ord 稳定身份），SQLite 不支持
    #    ALTER 改主键，必须"建新表 → 复制 → 改名"，见 _migrate_messages
    (6, None),
    # 7：_stats 拆出 messages.content，进独立表（token 三列可查询，其余字段
    #    进 stats_json 无损保留——前端回放要渲染耗时/缓存命中率/停止标记）
    (7, "CREATE TABLE IF NOT EXISTS message_usage(\n"
        "    session_id TEXT NOT NULL,\n"
        "    mid TEXT NOT NULL,\n"
        "    prompt_tokens INTEGER,\n"
        "    completion_tokens INTEGER,\n"
        "    cached_tokens INTEGER,\n"
        "    stats_json TEXT,\n"
        "    PRIMARY KEY(session_id, mid)\n"
        ")"),
    # 8：常驻事件流（SSE）的事件流水号 last_seq。每条业务事件发布后 +1 落库，
    #    重启后新事件从它继续、绝不归零——否则重启前后的事件会撞号，客户端
    #    的 seq 去重闸门会把新事件误判为"已应用过"而丢弃。注意 seq 是【事件】
    #    的流水号，与消息的 mid（稳定身份）是两套体系，互不通用。
    (8, "ALTER TABLE sessions ADD COLUMN last_seq INTEGER DEFAULT 0"),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migration_applied(conn: sqlite3.Connection, version: int) -> bool:
    """单条迁移的"已应用"探针：给老库对账用（列已在 = 当年那条散落 ALTER 补过）。
    user_version 是主判据，这里是防御——版本号丢失时重放也不会二次破坏。"""
    if version in (1,):        # providers.api_format
        return "api_format" in _table_columns(conn, "providers")
    if version == 2:           # sessions.user_id
        return "user_id" in _table_columns(conn, "sessions")
    if version == 3:           # provider_models.vision
        return "vision" in _table_columns(conn, "provider_models")
    if version == 4:           # sessions.workspace
        return "workspace" in _table_columns(conn, "sessions")
    if version == 5:           # providers.context_window
        return "context_window" in _table_columns(conn, "providers")
    if version == 6:           # messages 已是 mid/ord 新形态
        return "mid" in _table_columns(conn, "messages")
    if version == 7:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='message_usage'").fetchone())
    if version == 8:           # sessions.last_seq
        return "last_seq" in _table_columns(conn, "sessions")
    return False


def _migrate_messages(conn: sqlite3.Connection) -> None:
    """迁移 6：messages 从 (seq 自增主键, idx 位置) 重建为 (mid 稳定身份, ord 显示序)。

    为什么身份不能用 idx 位置充当：上下文压缩会往历史中段插入 role=compact
    边界消息，插入点之后所有消息的位置整体后移——拿位置当身份，同一轮增量写
    会把"第 5 条"写进原来"第 5 条"的行里，内容全部错位；身份必须与位置分离。

    旧数据的 mid 现生成（uuid4）、ord = 旧 idx * ORD_GAP：乘间隔而非原样照搬，
    是为了给未来的压缩边界留出"取中点"的空隙（连续编号 0,1,2 之间插不进任何
    整数）。全程一个事务：复制到一半失败时整体回滚，不会留下半新半旧的表。
    """
    cols = _table_columns(conn, "messages")
    if not cols:
        conn.execute(_MESSAGES_DDL)  # 理论到不了这里（_BASE_DDL 已建），防御
    elif "mid" in cols:
        pass  # 已是新形态（全新库由 _BASE_DDL 直接建好），只补索引即可
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE messages RENAME TO messages_legacy")
            conn.execute(_MESSAGES_DDL)
            rows = conn.execute(
                "SELECT session_id, idx, role, content FROM messages_legacy "
                "ORDER BY session_id, idx").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO messages(mid, session_id, ord, role, content) VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, r["session_id"], int(r["idx"]) * ORD_GAP,
                     r["role"], r["content"]),
                )
            conn.execute("DROP TABLE messages_legacy")
            conn.execute("COMMIT")
            log.info("messages 表迁移完成：%d 行旧数据已搬到 mid/ord 新形态", len(rows))
        except Exception:
            conn.execute("ROLLBACK")
            raise


def init_db() -> None:
    """建表 + 迁移 + 首次播种。每次服务启动时调用，幂等。"""
    with _conn() as conn:
        conn.executescript(_BASE_DDL)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version <= current or _migration_applied(conn, version):
                continue  # 已执行过（版本记账）或老库当年已补过列（探针对账）
            if sql is None:
                _migrate_messages(conn)
            else:
                conn.execute(sql)
                log.info("schema 迁移 v%d 已执行", version)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # (session_id, ord) 是 get_messages / 分页 / 窗口恢复的全部查询路径。
        # 放在迁移之后统一建：全新库的 messages 直接是最终形态（迁移 6 被跳过），
        # 老库迁移完也必有 ord 列；不能放进 _BASE_DDL——老库还没有 ord 列时会报错。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ord ON messages(session_id, ord)")
        # 播种：没有供应商时，把 .env 的配置导入为"默认"供应商
        if not conn.execute("SELECT 1 FROM providers LIMIT 1").fetchone():
            base_url = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
            api_key = os.environ.get("LLM_API_KEY", "")
            model = os.environ.get("LLM_MODEL", "glm-4-flash")
            window = int(os.environ.get("CONTEXT_WINDOW", "262144"))
            conn.execute(
                "INSERT INTO providers(id, name, base_url, api_key, enabled, created) VALUES(?,?,?,?,1,?)",
                ("default", "默认", base_url, api_key, time.time()),
            )
            conn.execute(
                "INSERT INTO provider_models(provider_id, name, context_window, enabled) VALUES(?,?,?,1)",
                ("default", model, window),
            )
        # 播种：没有激活模型设置时，指向默认供应商的第一个模型
        if not conn.execute("SELECT 1 FROM settings WHERE key='active_model'").fetchone():
            row = conn.execute("SELECT name FROM provider_models WHERE provider_id='default' LIMIT 1").fetchone()
            model = row["name"] if row else ""
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('active_model', ?)",
                (json.dumps({"provider_id": "default", "model": model}, ensure_ascii=False),),
            )


# ---------------------------------------------------------------------------
# 用户与登录令牌
# ---------------------------------------------------------------------------

_PBKDF2_ROUNDS = 120_000


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256，随机盐，格式：pbkdf2$轮数$盐hex$哈希hex。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def count_users() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, password: str) -> dict:
    """注册新用户，返回 {"id", "username"}；用户名已存在时抛 ValueError。"""
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError(f"用户名 {username} 已被占用")
        cur = conn.execute("INSERT INTO users(username, password_hash, created) VALUES(?,?,?)",
                           (username, hash_password(password), time.time()))
        return {"id": cur.lastrowid, "username": username}


def get_user_by_name(username: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT id, username, password_hash FROM users WHERE username=?",
                           (username,)).fetchone()
    return {"id": row["id"], "username": row["username"],
            "password_hash": row["password_hash"]} if row else None


def create_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with _conn() as conn:
        conn.execute("INSERT INTO auth_tokens(token, user_id, created) VALUES(?,?,?)",
                     (token, user_id, time.time()))
    return token


def user_for_token(token: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.username FROM auth_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token=?", (token,)).fetchone()
    return {"id": row["id"], "username": row["username"]} if row else None


def delete_token(token: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM auth_tokens WHERE token=?", (token,))


def claim_orphan_sessions(user_id: int) -> int:
    """把没有归属（user_id IS NULL）的旧任务划给该用户，返回划转数量。

    只在"第一个用户注册"时调用：升级前数据库里的任务都是机主本人的，
    让第一个注册账号（通常就是机主）接手，旧对话不至于凭空消失。
    """
    with _conn() as conn:
        cur = conn.execute("UPDATE sessions SET user_id=? WHERE user_id IS NULL", (user_id,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# 任务（会话）与消息
# ---------------------------------------------------------------------------

def create_session(sid: str, user_id: int, title: str = "") -> None:
    now = time.time()
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO sessions(id, title, user_id, created, updated) "
                     "VALUES(?,?,?,?,?)", (sid, title, user_id, now, now))


def session_owner(sid: str) -> int | None:
    with _conn() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE id=?", (sid,)).fetchone()
    return row["user_id"] if row else None


def set_session_title(sid: str, title: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))


def get_session_workspace(sid: str) -> str | None:
    """任务自选的工作区（用户在"选择工作区"弹窗里为该任务指定过才有值）。"""
    with _conn() as conn:
        row = conn.execute("SELECT workspace FROM sessions WHERE id=?", (sid,)).fetchone()
    return row["workspace"] if row else None


def set_session_workspace(sid: str, path: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE sessions SET workspace=? WHERE id=?", (path, sid))


def touch_session(sid: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time(), sid))


def get_last_seq(sid: str) -> int:
    """会话事件流（SSE）的最新 seq。会话不存在返回 0（等价于"从头开始"）。"""
    with _conn() as conn:
        row = conn.execute("SELECT last_seq FROM sessions WHERE id=?", (sid,)).fetchone()
    return int(row["last_seq"]) if row and row["last_seq"] is not None else 0


def set_last_seq(sid: str, seq: int) -> None:
    """记录会话事件流的最新 seq（每条事件发布后调用一次）。
    WAL + synchronous=NORMAL 下一行 UPDATE 是微秒级，流式 delta 的间隔
    （几十毫秒）里绰绰有余，换来"客户端见过的 seq 重启后绝不回退"的强保证。
    标量 MAX 保证只前进：乱序到达的旧值不会把计数器拉回去。"""
    with _conn() as conn:
        conn.execute("UPDATE sessions SET last_seq=MAX(COALESCE(last_seq,0),?) WHERE id=?",
                     (int(seq), sid))


def list_sessions(user_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT id, title, updated FROM sessions WHERE user_id=? "
                            "ORDER BY updated DESC", (user_id,)).fetchall()
    # 注意：返回原始 title（可能为空），"新任务"之类的展示兜底交给前端做。
    # 之前在这里兜底，导致"标题为空→设标题"的判断永远不成立，标题永远存不上。
    return [{"id": r["id"], "title": r["title"], "updated": r["updated"]} for r in rows]


def delete_session(sid: str) -> None:
    """删除任务：消息行、统计行、外置正文文件一起清理。"""
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM message_usage WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    # artifacts/<sid>/ 整棵删掉（该任务全部外置正文，随会话一起消失）
    shutil.rmtree(_artifacts_dir() / sid, ignore_errors=True)


# ---------------------------------------------------------------------------
# 消息存取：稳定身份 + 增量落盘 + 大内容外置 + 按窗口加载
# ---------------------------------------------------------------------------

def _artifacts_dir() -> Path:
    # 跟着库文件走（DB_PATH.parent/artifacts）：测试重定向 DB_PATH 时，
    # 归档目录也随行到临时目录，不污染项目根。
    return DB_PATH.parent / "artifacts"


def _storable_body(m: dict) -> dict:
    """消息的"可存体"：剥掉 _mid/_ord（在列里有专位）与 _stats（在
    message_usage 表）。指纹与落库内容都基于它，保证两处序列化字节一致。"""
    return {k: v for k, v in m.items() if k not in ("_mid", "_ord", "_stats")}


def _fingerprint(blob: str) -> str:
    """内容指纹：对将写入 content 列的序列化字节求 sha1。"""
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def fingerprints(msgs: list[dict]) -> dict[str, str]:
    """从（恢复出来的）消息列表重建 {mid: 指纹} 字典。

    进程重启后 Agent 实例的 saved 字典丢失，用它重建——只覆盖"当前在内存
    窗口里"的消息即可：save_messages 只会查询历史里出现过的 mid，窗口之外
    的行根本不会被触碰。指纹必须与 save_messages 落库时的算法完全一致
    （同用 _storable_body + json.dumps），否则重启后第一轮会误判"全变了"
    而重写全表。
    """
    out: dict[str, str] = {}
    for m in msgs:
        mid = m.get("_mid")
        if not mid:
            continue  # 不带身份的消息（理论不存在）交给 save 重新分配
        out[mid] = _fingerprint(json.dumps(_storable_body(m), ensure_ascii=False))
    return out


def _assign_ords(conn: sqlite3.Connection, sid: str, history: list[dict],
                 _renumbered: bool = False) -> None:
    """给历史里没有 _ord 的消息分配显示序（就地写回消息本体）。

    新消息只有两种来路，对应两种分配方式：
    a) 尾部连续追加（普通的一轮对话）——从最后一个已知 ord 起按 ORD_GAP 递增；
    b) 夹在两条已知 ord 消息之间的孤立新消息（压缩刚插入的边界）——取两侧
       ord 的中点：唯一、单调、且不打扰任何已落盘消息的序号（间隔预留的
       目的就在这里，见 ORD_GAP 注释）。

    间隔耗尽兜底：中点插入每次把间隔对半，同一对消息之间反复插入会让间隔
    1024/2^k 递减直至相邻。实测当前 _maybe_compact 不会走到这一步——插入点
    每轮至少前移 5 个槽位（cut ≥ head+MIN_SEGMENT，且活区起点在上一条边界
    之后），同一对消息至多被插入一次，间隔最小折半到 512。但 _assign_ords
    是存储层，它的正确性不该依赖 agent.py 插入模式这条【隐性跨模块不变式】：
    换压缩策略、窗口恢复让锚点与边界在内存中相邻后再插入、防御分支命中，
    都可能让同一对消息被反复插边界。而 ord 没有唯一约束，间隔耗尽后的中点
    是【静默并列】而非报错——分页游标 ord<before_ord 会漏条目或打转，排序
    正确性退化为靠 rowid 并列兜底。所以在真正写出并列之前触发整会话重编号
    （_renumber_session_ords），把全部消息按当前顺序重排为 ORD_GAP 的倍数，
    再重新放置新消息。

    为什么重编号这个操作可接受：
    1. 触发频率趋近于零（现有压缩策略下不可达，纯防御），且一旦触发、间隔
       重新铺满 ORD_GAP，几十次压缩内不会再触发第二次；
    2. 有界：只影响单个会话，一次性 O(N) 行 UPDATE（N=该会话消息数），
       代价上封顶——对比的是"并列 ord 静默破坏分页与排序"这种数据级后果；
    3. 重编号只动 ord 列。_ord 不进 content、不进内容指纹（指纹只算 content
       字节），因此已有消息的内容零重写；若未来把 ord 编进 content/指纹，
       重编号必须发生在指纹比较之前——save_messages 先 _assign_ords 再算
       指纹的调用顺序已经保证了这一点。
    """
    if not history:
        return
    last_known = -1
    for i, m in enumerate(history):
        if "_ord" in m:
            last_known = i
    if last_known < 0:
        # 整段都没有 ord（全新会话）。兜底查一次库：极端情况下内存历史与库
        # 脱节（不该发生），从库内最大 ord 之后接续，好过从 0 起撞车。
        row = conn.execute("SELECT MAX(ord) FROM messages WHERE session_id=?", (sid,)).fetchone()
        nxt = (row[0] + ORD_GAP) if row and row[0] is not None else 0
        for m in history:
            m["_ord"] = nxt
            nxt += ORD_GAP
        return
    nxt = history[last_known]["_ord"] + ORD_GAP
    for m in history[last_known + 1:]:
        m["_ord"] = nxt
        nxt += ORD_GAP
    for i in range(last_known):
        m = history[i]
        if "_ord" in m:
            continue
        nxt_has = history[i + 1].get("_ord")
        prev_ord = history[i - 1]["_ord"] if i > 0 else (
            (nxt_has - ORD_GAP) if nxt_has is not None else 0)
        if nxt_has is None:
            m["_ord"] = prev_ord + ORD_GAP  # 防御：连续多条中段新消息，按追加链排
            continue
        if nxt_has - prev_ord <= 1:
            # 间隔耗尽（相邻，插不进任何整数）。重编号后重启整个放置流程；
            # 二次耗尽 = 数据已不一致（重编号铺满 ORD_GAP 后不可能再相邻），
            # 此刻静默写出并列是最坏选择，响亮失败留给排查。
            if _renumbered:
                raise RuntimeError(f"会话 {sid} 重编号后仍无 ord 间隔可用，数据疑似不一致")
            _renumber_session_ords(conn, sid, history)
            _assign_ords(conn, sid, history, _renumbered=True)
            return
        m["_ord"] = (prev_ord + nxt_has) // 2  # 压缩边界：取两侧中点


def _renumber_session_ords(conn: sqlite3.Connection, sid: str, history: list[dict]) -> None:
    """间隔耗尽兜底：把该会话【全部】消息按当前顺序重排为 ORD_GAP 的倍数。

    以数据库为准重排（窗口恢复时内存只是子集——锚点 + 边界之后的尾部，
    边界之前的消息不在内存里，只改内存必然与库内行撞号），再按 mid 把新序
    同步回内存历史。排序键 (ord, rowid) 与 get_messages 完全一致：即便库中
    已存在并列 ord（旧代码写出的），两处看到的顺序也相同，重排不会翻转
    时间线。重编号只 UPDATE ord 列——content 与指纹不动，已有消息的内容
    零重写（见 _assign_ords 的论证）。

    重排后记入 renumbered_sessions：app 层消费这个标记通知前端"分页游标已
    失效、请重拉时间线"（before_ord 游标指向的是旧序号值，新序号空间里它
    落在哪完全随机，继续用会漏条目或重复）。
    """
    rows = conn.execute("SELECT mid FROM messages WHERE session_id=? ORDER BY ord, rowid",
                        (sid,)).fetchall()
    fixed = {}
    for i, r in enumerate(rows):
        fixed[r["mid"]] = i * ORD_GAP
        conn.execute("UPDATE messages SET ord=? WHERE session_id=? AND mid=?",
                     (i * ORD_GAP, sid, r["mid"]))
    for m in history:
        mid = m.get("_mid")
        if mid in fixed:
            m["_ord"] = fixed[mid]
    renumbered_sessions.add(sid)
    log.warning("会话 %s 的 ord 间隔耗尽，已整会话重编号（%d 行，前端分页游标失效）",
                sid, len(rows))


# app 层消费：本轮 save_messages 是否触发过整会话重编号（消费后 discard）。
# 挂在模块级而不是返回值里：save_messages 的返回值是"写入行数"，语义已被
# 验收指标占用；重编号是罕见旁路事件，用集合把信号带给调用方最省事。
renumbered_sessions: set[str] = set()


def _write_artifact(sid: str, mid: str, blob: str) -> tuple[str, int]:
    """把超大消息的完整 JSON 写进 artifacts/<sid>/<mid>.json，返回 (相对路径, 字节数)。

    先写 .tmp 再 os.replace 原子改名：os.replace 在同一文件系统上是原子的，
    写到一半断电/崩溃只会留下一个孤儿 .tmp，绝不会让归档文件出现"半个 JSON"。
    """
    d = _artifacts_dir() / sid
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{mid}.json"
    tmp = d / f"{mid}.json.tmp"
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, path)
    # 相对路径以 artifacts 目录为根（read_artifact 的校验基准也是它）
    return f"{sid}/{mid}.json", len(blob.encode("utf-8"))


def _upsert_usage(conn: sqlite3.Connection, sid: str, mid: str, stats: dict) -> None:
    """_stats 落到 message_usage：token 三列按表结构展开（可查询、可聚合），
    完整原样进 stats_json（elapsed_s/cache_hit_rate/stopped 等前端回放要用的
    字段一个不丢）。"""
    usage = stats.get("usage") or {}
    conn.execute(
        "INSERT OR REPLACE INTO message_usage(session_id, mid, prompt_tokens, completion_tokens, "
        "cached_tokens, stats_json) VALUES(?,?,?,?,?,?)",
        (sid, mid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
         usage.get("prompt_cache_hit_tokens"), json.dumps(stats, ensure_ascii=False)),
    )


def save_messages(sid: str, history: list[dict], saved: dict) -> int:
    """增量落盘一轮消息，返回实际写入的行数。

    saved 是 {mid: 内容指纹}，由调用方（Agent 实例）跨轮持有；只写「不在
    saved 中」或「指纹有变化」的消息（INSERT OR REPLACE，主键 (session_id,
    mid) 保证同一 mid 重复落盘不产生重复行），不再 DELETE 全表——旧方案每轮
    重写整段历史，写放大随会话长度是 O(n²)，这是本函数要根治的问题。

    附带两个副作用（都有明确收益）：
    1. 超过 MAX_INLINE_BYTES 的消息：完整 JSON 外置到 artifacts 文件，行内
       只留 head/tail 摘要，并且【就地】把内存里那条消息替换成摘要行——内存
       不再扛着 1MB 的工具输出，模型视图按需从归档还原（Agent._expand_artifact）；
    2. _stats 拆进 message_usage 表，content 行不再夹带统计。
    """
    if not history:
        return 0
    written = 0
    with _conn() as conn:
        # 会话可能在流式进行中被删除，此时不再写入（否则会留下孤儿消息行）
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            return 0
        _assign_ords(conn, sid, history)
        for m in history:
            mid = m.get("_mid")
            if not mid:
                mid = uuid.uuid4().hex
                m["_mid"] = mid  # 身份写回消息本体：下一轮它仍然是"同一条"消息
            ord_v = m["_ord"]
            blob = json.dumps(_storable_body(m), ensure_ascii=False)
            fp = _fingerprint(blob)
            if saved.get(mid) == fp:
                continue  # 增量核心：库里已是这份内容（身份在、指纹同），跳过
            if m.get("_stats") is not None:
                _upsert_usage(conn, sid, mid, m["_stats"])
            if len(blob.encode("utf-8")) > MAX_INLINE_BYTES:
                rel, nbytes = _write_artifact(sid, mid, blob)
                stub = {"_artifact": True, "path": rel, "bytes": nbytes,
                        "head": blob[:2000], "tail": blob[-1000:],
                        "role": m.get("role", "")}
                row_content = json.dumps(stub, ensure_ascii=False)
                fp = _fingerprint(row_content)
                # 就地替换成摘要行：指纹改按"摘要行"记，下一轮重放这段历史
                # （无论来自内存还是 get_messages 恢复）都能对上、不再重写。
                m.clear()
                m.update(stub)
                m["_mid"], m["_ord"] = mid, ord_v
            else:
                row_content = blob
            conn.execute(
                "INSERT OR REPLACE INTO messages(mid, session_id, ord, role, content) "
                "VALUES(?,?,?,?,?)",
                (mid, sid, ord_v, m.get("role", ""), row_content),
            )
            saved[mid] = fp
            written += 1
    return written


def read_artifact(rel_path: str) -> dict:
    """读取外置归档，还原完整消息（dict）。

    路径校验（防逃逸）：rel_path 来自消息行/前端查询参数，是不可信输入。
    攻击场景：登录用户把 path 参数改成 "../../../.env" 就能借本接口读到库
    目录之外的任意文件（API Key、系统文件）；绝对路径在 pathlib 拼接时会
    【整个替换掉】基准目录，同样越界；符号链接则可能在 resolve 前指向外部。
    因此：realpath 消解（压平 ../ 与软链）之后，结果必须仍严格位于 artifacts
    目录之内，且以 .json 结尾——白名单式的双重校验，不满足即拒绝并记日志。
    """
    base = _artifacts_dir().resolve()
    p = (base / rel_path).resolve()
    if p == base or base not in p.parents or p.suffix != ".json":
        log.warning("read_artifact 拒绝越界路径: %r", rel_path)
        raise ValueError("非法的归档路径")
    return json.loads(p.read_text(encoding="utf-8"))


def get_messages(sid: str, since_ord: int | None = None,
                 before_ord: int | None = None, limit: int | None = None) -> list[dict]:
    """按 ord 升序取回消息（自动回填 _mid/_ord/_stats）。

    参数全不传 = 全量加载（兼容旧行为）；since_ord/before_ord/limit 供窗口
    化：since_ord 取"该序及之后"（压缩边界起），before_ord 取"严格之前"
    （向上翻页），limit 语义是"最近 limit 条"（先降序取满一页再反转回升序，
    时间线分页要的是尾部一页，不是头部）。
    """
    with _conn() as conn:
        sql = "SELECT mid, ord, role, content FROM messages WHERE session_id=?"
        params: list = [sid]
        if since_ord is not None:
            sql += " AND ord >= ?"
            params.append(since_ord)
        if before_ord is not None:
            sql += " AND ord < ?"
            params.append(before_ord)
        if limit is not None:
            # (ord, rowid) 双键：ord 并列时按插入序兜底。健康库不会并列（重编号
            # 兜底保证），但旧数据可能已带并列——排序键必须与 _renumber_session_ords
            # 一致，否则"读的顺序"和"重排的顺序"可能翻转时间线
            sql += " ORDER BY ord DESC, rowid DESC LIMIT ?"
            params.append(int(limit))
        else:
            sql += " ORDER BY ord, rowid"
        rows = conn.execute(sql, params).fetchall()
        if limit is not None:
            rows = list(reversed(rows))  # 回升序：时间线自上而下渲染
        usage = {r["mid"]: r for r in conn.execute(
            "SELECT mid, stats_json, prompt_tokens, completion_tokens, cached_tokens "
            "FROM message_usage WHERE session_id=?", (sid,))}
    msgs = []
    for r in rows:
        msg = json.loads(r["content"])
        msg["_mid"], msg["_ord"] = r["mid"], r["ord"]
        u = usage.get(r["mid"])
        if u is not None:
            # 优先 stats_json（无损全量）；极端情况下只有列（手写库）再降级拼装
            msg["_stats"] = (json.loads(u["stats_json"]) if u["stats_json"] else
                             {"usage": {"prompt_tokens": u["prompt_tokens"],
                                        "completion_tokens": u["completion_tokens"],
                                        "cached_tokens": u["cached_tokens"]}})
        msgs.append(msg)
    return msgs


def compact_boundary_ord(sid: str) -> int | None:
    """最后一条 role=compact 压缩边界的 ord（从未压缩过返回 None）。"""
    with _conn() as conn:
        row = conn.execute("SELECT MAX(ord) FROM messages WHERE session_id=? AND role='compact'",
                           (sid,)).fetchone()
    return row[0] if row and row[0] is not None else None


def first_user_message(sid: str) -> dict | None:
    """会话最初一条用户消息（模型视图的锚点）。

    _visible_history 逐字保留它作为整个任务的原始需求——窗口恢复时它落在
    压缩边界之前、本不该进内存，但缺了它，恢复后的历史里"第一条 user"会
    变成边界之后的消息：锚点丢失且该消息在视图里重复出现（既当锚点又在
    保留段里）。所以窗口恢复必须额外捎上这一条（见 restore_window）。
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT mid, ord, role, content FROM messages WHERE session_id=? AND role='user' "
            "ORDER BY ord LIMIT 1", (sid,)).fetchone()
        if row is None:
            return None
        msg = json.loads(row["content"])
        msg["_mid"], msg["_ord"] = row["mid"], row["ord"]
        u = conn.execute("SELECT stats_json, prompt_tokens, completion_tokens, cached_tokens "
                         "FROM message_usage WHERE session_id=? AND mid=?", (sid, row["mid"])).fetchone()
    if u is not None and u["stats_json"]:
        msg["_stats"] = json.loads(u["stats_json"])
    return msg


def restore_window(sid: str) -> list[dict]:
    """会话恢复用的窗口加载：从未压缩 = 全量；压缩过 = 锚点 + 最后一条边界
    及其之后的消息（边界摘要是后续再压缩的输入，必须加载）。

    收益：恢复的内存占用与当前窗口成正比，而非全会话长度——被压缩段的
    原文仍留在库里（真相不动），但不再整段进内存；模型视图与全量恢复
    逐字节一致（锚点 + 摘要 + 活区，见 _visible_history 的结构）。
    """
    b = compact_boundary_ord(sid)
    if b is None:
        return get_messages(sid)
    msgs = get_messages(sid, since_ord=b)
    anchor = first_user_message(sid)
    if anchor is not None and anchor["_ord"] < b:
        msgs.insert(0, anchor)
    return msgs


def count_messages(sid: str) -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                            (sid,)).fetchone()[0]


def has_messages_before(sid: str, ord_value: int) -> bool:
    """是否还有 ord 更小的消息（时间线分页判断 has_more 用）。"""
    with _conn() as conn:
        return bool(conn.execute("SELECT 1 FROM messages WHERE session_id=? AND ord<? LIMIT 1",
                                 (sid, ord_value)).fetchone())


# ---------------------------------------------------------------------------
# 供应商与模型
# ---------------------------------------------------------------------------

def list_providers() -> list[dict]:
    with _conn() as conn:
        provs = conn.execute("SELECT * FROM providers ORDER BY created").fetchall()
        models = conn.execute("SELECT * FROM provider_models ORDER BY name").fetchall()
    result = []
    for p in provs:
        result.append({
            "id": p["id"], "name": p["name"], "base_url": p["base_url"],
            "api_format": p["api_format"] or "openai",
            "api_key": p["api_key"], "enabled": bool(p["enabled"]),
            "context_window": p["context_window"] or 128000,  # 供应商级默认窗口（ALTER 补列，旧行也有值）
            "models": [{"name": m["name"], "context_window": m["context_window"],
                        "enabled": bool(m["enabled"]), "vision": bool(m["vision"])}
                       for m in models if m["provider_id"] == p["id"]],
        })
    return result


def get_provider(pid: str) -> dict | None:
    for p in list_providers():
        if p["id"] == pid:
            return p
    return None


def upsert_provider(pid: str, name: str, base_url: str, api_key: str | None, enabled: bool,
                    api_format: str | None = None, context_window: int | None = None) -> None:
    # context_window：None = 保持原值不变（前端编辑时不填窗口就不动它）
    with _conn() as conn:
        exists = conn.execute("SELECT 1 FROM providers WHERE id=?", (pid,)).fetchone()
        fmt = api_format if api_format in ("openai", "anthropic") else None
        if exists:
            conn.execute("UPDATE providers SET name=?, base_url=?, enabled=? WHERE id=?",
                         (name, base_url, int(enabled), pid))
            if api_key is not None:  # None = 保持原 key 不变
                conn.execute("UPDATE providers SET api_key=? WHERE id=?", (api_key, pid))
            if fmt is not None:
                conn.execute("UPDATE providers SET api_format=? WHERE id=?", (fmt, pid))
            if context_window is not None:
                conn.execute("UPDATE providers SET context_window=? WHERE id=?", (context_window, pid))
        else:
            conn.execute("INSERT INTO providers(id, name, base_url, api_format, api_key, enabled, "
                         "context_window, created) VALUES(?,?,?,?,?,?,?,?)",
                         (pid, name, base_url, fmt or "openai", api_key or "", int(enabled),
                          context_window if context_window is not None else 128000, time.time()))


def delete_provider(pid: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM provider_models WHERE provider_id=?", (pid,))
        conn.execute("DELETE FROM providers WHERE id=?", (pid,))


def upsert_model(pid: str, name: str, context_window: int, enabled: bool,
                 vision: bool | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO provider_models(provider_id, name, context_window, enabled, vision) VALUES(?,?,?,?,?) "
            "ON CONFLICT(provider_id, name) DO UPDATE SET context_window=excluded.context_window, "
            "enabled=excluded.enabled, vision=COALESCE(?, provider_models.vision)",
            (pid, name, context_window, int(enabled), 0 if vision is None else int(vision),
             None if vision is None else int(vision)),
        )


def delete_model(pid: str, name: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM provider_models WHERE provider_id=? AND name=?", (pid, name))


# ---------------------------------------------------------------------------
# 键值设置（当前激活模型）
# ---------------------------------------------------------------------------

def get_setting(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def ensure_active_model() -> None:
    """激活模型指向的供应商/模型被改名或删除后，自动纠正到第一个可用项。

    没有这一步：用户在管理面板里给模型改名后，settings 里的激活记录仍指向旧名，
    聊天时虽然能兜底回退，但会话/工具栏会一直处于"指向失效模型"的状态。
    """
    active = get_setting("active_model") or {}
    prov = get_provider(active.get("provider_id", ""))
    if prov is None or not prov["enabled"]:
        first = next((p for p in list_providers()
                      if p["enabled"] and any(m["enabled"] for m in p["models"])), None)
        if first:
            set_setting("active_model", {"provider_id": first["id"],
                                         "model": next(m["name"] for m in first["models"] if m["enabled"])})
        return
    enabled_names = [m["name"] for m in prov["models"] if m["enabled"]]
    if active.get("model") not in enabled_names and enabled_names:
        set_setting("active_model", {"provider_id": prov["id"], "model": enabled_names[0]})
