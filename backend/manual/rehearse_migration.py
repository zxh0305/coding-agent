"""
迁移演练脚本：在真实库的【副本】上完整走一遍迁移 + 增量落盘 + 窗口恢复 + 新接口冒烟。
验收通过后才允许对真库执行 init_db()（见脚本末尾说明，演练脚本自身绝不碰真库）。
运行：cd backend && python3 manual/rehearse_migration.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # 脚本在 manual/ 子目录，退两级到 backend/
import db

REAL_DB = Path(db.DB_PATH)
WORK = Path(tempfile.mkdtemp(prefix="migrate_rehearsal_"))
COPY = WORK / "rehearsal.db"

# 1) 用 SQLite backup API 拷贝真库（顺带把 WAL 里的内容合并进副本，比 cp 三个文件可靠）
src = sqlite3.connect(REAL_DB)
dst = sqlite3.connect(COPY)
src.backup(dst)
dst.close(); src.close()

before_rows = sqlite3.connect(COPY).execute("SELECT COUNT(*) FROM messages").fetchone()[0]
before_sessions = sqlite3.connect(COPY).execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
before_version = sqlite3.connect(COPY).execute("PRAGMA user_version").fetchone()[0]
print(f"[演练库] 迁移前：messages={before_rows} sessions={before_sessions} user_version={before_version}")

# 2) 在副本上执行迁移（DB_PATH 重定向到副本，绝不碰真库）
db.DB_PATH = COPY
db.init_db()

after = sqlite3.connect(COPY)
after_rows = after.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
after_version = after.execute("PRAGMA user_version").fetchone()[0]
cols = [r[1] for r in after.execute("PRAGMA table_info(messages)")]
dup = after.execute("SELECT COUNT(*) FROM (SELECT mid FROM messages GROUP BY session_id, mid "
                    "HAVING COUNT(*) > 1)").fetchone()[0]
print(f"[演练库] 迁移后：messages={after_rows}（条数不变={after_rows == before_rows}）"
      f" user_version={after_version} 列={cols} 重复mid={dup}")

# 3) 幂等：再跑一遍，行数/mid 集合/版本号完全不变
mids_before = sorted(r[0] for r in after.execute("SELECT mid FROM messages"))
db.init_db()
after2 = sqlite3.connect(COPY)
mids_after = sorted(r[0] for r in after2.execute("SELECT mid FROM messages"))
print(f"[演练库] 二次迁移幂等：行数一致={after2.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == after_rows}"
      f" mid集合一致={mids_before == mids_after}"
      f" 版本一致={after2.execute('PRAGMA user_version').fetchone()[0] == after_version}")

# 4) 真实会话验证（用副本里的真实数据；必须 JOIN sessions——库里存在
#    会话行已删、消息残留的孤儿组，防孤儿守卫会正确拒写它们）
sid = after2.execute("SELECT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
                     "GROUP BY m.session_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
total = db.count_messages(sid)
msgs = db.get_messages(sid)
print(f"\n[真实会话 {sid}] 全量消息 {total} 条，读取 {len(msgs)} 条")

# 4a) 注入一条压缩边界（走真实的 save_messages 增量路径）再追加一轮
msgs.insert(max(1, len(msgs) // 2), {"role": "compact", "content": "演练注入的早期摘要",
                                     "is_compact_boundary": True, "_stats": {"compacted": True}})
msgs.append({"role": "user", "content": "演练：边界后的新问题"})
msgs.append({"role": "assistant", "content": "演练：边界后的新回答",
             "_stats": {"elapsed_s": 1.0, "usage": {"prompt_tokens": 9, "completion_tokens": 9}}})
saved = db.fingerprints(db.get_messages(sid))  # 重启模拟：账本由存量重建
written = db.save_messages(sid, msgs, saved)
print(f"[真实会话 {sid}] 注入边界+追加 3 条 → 写入 {written} 行（期望 3：存量 0 重写）")

# 4b) 恢复：实际加载条数 vs 边界后条数
b = db.compact_boundary_ord(sid)
window = db.restore_window(sid)
after_b = len(db.get_messages(sid, since_ord=b))
expect = after_b + 1  # 边界及其之后 + 锚点 1 条
print(f"[真实会话 {sid}] 恢复加载 {len(window)} 条 = 边界后 {after_b} 条 + 锚点 1 条"
      f"（总会话 {db.count_messages(sid)} 条）断言={'PASS' if len(window) == expect else 'FAIL'}")

# 4c) 第二轮起写入行数 = 新增条数
for rnd in (2, 3):
    msgs.append({"role": "user", "content": f"演练第 {rnd} 轮问题"})
    msgs.append({"role": "assistant", "content": f"演练第 {rnd} 轮回答"})
    w = db.save_messages(sid, msgs, saved)
    print(f"[真实会话 {sid}] 第 {rnd} 轮写入 {w} 行（期望 2）断言={'PASS' if w == 2 else 'FAIL'}")

# 5) 新接口冒烟：起真实 HTTP 服务打一遍分页/归档接口
import urllib.parse

import app as app_mod
db.create_user("rehearsal_user", "pass1234")
uid = db.get_user_by_name("rehearsal_user")["id"]
with sqlite3.connect(COPY) as conn:  # with：用完提交并释放连接，别把写锁带过夜
    conn.execute("UPDATE sessions SET user_id=? WHERE id=?", (uid, sid))
db.create_session("s_art", uid)
big = {"role": "assistant", "content": "归" * (db.MAX_INLINE_BYTES + 1024)}
db.save_messages("s_art", [{"role": "user", "content": "q"}, big], {})
token = db.create_token(uid)

server = app_mod.ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
port = server.server_address[1]
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(0.3)

def get(path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

page = get(f"/api/sessions/{sid}/messages?limit=5")
print(f"\n[接口] 分页第一页：{len(page['messages'])} 条 has_more={page['has_more']}")
older = get(f"/api/sessions/{sid}/messages?limit=5&before_ord={page['messages'][0]['ord']}")
print(f"[接口] 向上翻页：{len(older['messages'])} 条 has_more={older['has_more']}")
full = get(f"/api/sessions/{sid}/messages")
print(f"[接口] 全量默认页：{len(full['messages'])} 条")
art_page = get("/api/sessions/s_art/messages")
am = art_page["messages"][-1]
print(f"[接口] 归档消息字段：artifact={am.get('artifact')} bytes={am.get('bytes')} path={am.get('path')}")
detail = get(f"/api/sessions/s_art/artifact?path={urllib.parse.quote(am['path'])}")
ok = detail["message"]["content"] == "归" * (db.MAX_INLINE_BYTES + 1024)
print(f"[接口] 归档全文取回一致={ok}")
try:
    get(f"/api/sessions/s_art/artifact?path=../../.env")
    print("[接口] 越界路径未被拒绝：FAIL")
except urllib.error.HTTPError as e:
    print(f"[接口] 越界路径被拒：HTTP {e.code} PASS")
try:
    get(f"/api/sessions/{sid}/artifact?path=s_art/{am['path'].split('/')[1]}")
    print("[接口] 跨任务归档路径未被拒绝：FAIL")
except urllib.error.HTTPError as e:
    print(f"[接口] 跨任务归档路径被拒：HTTP {e.code} PASS")

server.shutdown()
import shutil as _sh
print(f"\n演练库大小：{COPY.stat().st_size} 字节；真库大小：{REAL_DB.stat().st_size} 字节")
print("演练完成，临时目录：" + str(WORK))
