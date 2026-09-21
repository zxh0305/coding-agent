"""
数据持久层（SQLite，标准库 sqlite3，零依赖）
=============================================

早期版本把对话存在服务进程的内存 dict 里，重启即丢；现在统一落盘到项目根目录的
agent_data.db（SQLite 单文件数据库，可直接用任何 SQLite 工具打开查看）：

  users            登录用户：用户名 / 密码哈希（PBKDF2，不存明文）
  auth_tokens      登录令牌：随机 token -> 用户，重启不失效
  sessions         任务（会话）：标题、创建/更新时间、归属用户、各自的工作区
  messages         每个任务的完整消息历史（OpenAI 消息格式的 JSON，按顺序）
  providers        模型供应商：名称 / Base URL / API 格式 / API Key / 启用状态 / 默认窗口
  provider_models  供应商下的模型：模型名 / 上下文窗口 / 启用 / 是否支持视觉
  settings         键值设置（当前激活的模型等）

首库自动播种：providers 表为空时，把 .env 里的 LLM_* 配置导入为"默认"供应商。
关于 API Key：以明文存在本机数据库里（学习项目的务实选择），接口回显一律打码。
用户体系：登录只做身份区分与会话隔离（任务列表按用户过滤），供应商/模型
仍是全局共享——所有登录用户共用服务端配置的 LLM Key；工作区按任务隔离
（每个任务可有自己的工作区，切换互不影响）。
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "agent_data.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL：写不阻塞读。生成结束会把整段历史一次性落盘（replace_messages），
    # 默认 journal 模式下这期间其他请求的读写都要排队；WAL 让它们互不等待。
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL 推荐档位：断电最多丢最后一次写入，库文件本身不会损坏
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """建表 + 首次播种。每次服务启动时调用，幂等。"""
    with _conn() as conn:
        conn.executescript(
            """
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
                updated REAL
            );
            CREATE TABLE IF NOT EXISTS messages(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, idx);
            CREATE TABLE IF NOT EXISTS providers(
                id TEXT PRIMARY KEY,
                name TEXT,
                base_url TEXT,
                api_format TEXT DEFAULT 'openai',
                api_key TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
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
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # 旧库升级：给 providers 补 api_format 列（已存在则忽略）
        try:
            conn.execute("ALTER TABLE providers ADD COLUMN api_format TEXT DEFAULT 'openai'")
        except sqlite3.OperationalError:
            pass
        # 旧库升级：给 sessions 补 user_id 列。已有的旧任务没有归属（NULL），
        # 由第一个注册的用户认领（见 claim_orphan_sessions），之后各看各的。
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # 旧库升级：给 provider_models 补 vision 列（该模型是否支持视觉输入，
        # 由用户在管理面板标注；analyze_image 工具据此挑选"替主模型看图"的模型）
        try:
            conn.execute("ALTER TABLE provider_models ADD COLUMN vision INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # 旧库升级：给 sessions 补 workspace 列（每任务各自的工作区，NULL = 未指定，
        # 用"用户默认 → .env/项目默认"链解析）。此前工作区是全局环境变量，任何
        # 用户一切换、所有会话立即跟着变，并发生成的 Agent 会互相踩目录。
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN workspace TEXT")
        except sqlite3.OperationalError:
            pass
        # 旧库升级：给 providers 补 context_window 列（供应商级默认窗口，作为
        # 上下文压缩触发线的基准）。窗口解析链是"模型自填 → 供应商默认 → .env"：
        # 模型粒度已有一列，但要求"每家新模型都得手填窗口"太烦，漏填时退到这里。
        # 默认 128000 而非 262144：宁可早点触发压缩，也不要等真爆窗口了才动手。
        try:
            conn.execute("ALTER TABLE providers ADD COLUMN context_window INTEGER DEFAULT 128000")
        except sqlite3.OperationalError:
            pass
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


def list_sessions(user_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT id, title, updated FROM sessions WHERE user_id=? "
                            "ORDER BY updated DESC", (user_id,)).fetchall()
    # 注意：返回原始 title（可能为空），"新任务"之类的展示兜底交给前端做。
    # 之前在这里兜底，导致"标题为空→设标题"的判断永远不成立，标题永远存不上。
    return [{"id": r["id"], "title": r["title"], "updated": r["updated"]} for r in rows]


def delete_session(sid: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))


def replace_messages(sid: str, history: list[dict]) -> None:
    """用 Agent 当前的完整消息历史覆盖该任务在库里的记录（每次回答结束后整体落盘）。"""
    with _conn() as conn:
        # 会话可能在流式进行中被删除，此时不再写入（否则会留下孤儿消息行）
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            return
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        for idx, m in enumerate(history):
            conn.execute(
                "INSERT INTO messages(session_id, idx, role, content) VALUES(?,?,?,?)",
                (sid, idx, m.get("role", ""), json.dumps(m, ensure_ascii=False)),
            )


def get_messages(sid: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT content FROM messages WHERE session_id=? ORDER BY idx", (sid,)).fetchall()
    return [json.loads(r["content"]) for r in rows]


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
