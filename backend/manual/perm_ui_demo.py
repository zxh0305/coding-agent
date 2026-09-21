"""
权限卡片 UI 演示服务（手动验收用，跑起来后用浏览器操作）
==========================================================

python3 backend/manual/perm_ui_demo.py [端口]     默认 8765

临时库 + mock LLM + 真实 app 服务，常驻前台直到 Ctrl+C。剧本与
manual_permissions_check.py 相同：发「请清理临时目录 <TARGET>」→ mkdir 弹卡
→ rm -rf 弹卡。TARGET 在 /tmp/perm_ui_demo_target，由脚本创建（含 keep.txt）。
"""

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent  # 脚本在 manual/ 子目录，退两级到 backend/
sys.path.insert(0, str(BACKEND_DIR))

import db                      # noqa: E402
import manual_permissions_check as m   # noqa: E402  （复用其 mock LLM 剧本）

tmp = Path("/tmp/perm_ui_demo_ws")
tmp.mkdir(parents=True, exist_ok=True)
TARGET = Path("/tmp/perm_ui_demo_target")
m.TARGET = TARGET
(TARGET / "sub").mkdir(parents=True, exist_ok=True)
(TARGET / "sub" / "keep.txt").write_text("活口", encoding="utf-8")

db.DB_PATH = tmp / "ui.db"
db.init_db()
if db.get_user_by_name("demo") is None:
    user = db.create_user("demo", "demo1234")
else:
    user = db.get_user_by_name("demo")
db.set_setting(f"default_workspace:{user['id']}", str(tmp / "workspace"))
token = db.create_token(user["id"])

mock_server = ThreadingHTTPServer(("127.0.0.1", 0), m.MockLLMHandler)
mock_port = mock_server.server_address[1]
threading.Thread(target=mock_server.serve_forever, daemon=True).start()

db.upsert_provider("mock", "MockLLM", f"http://127.0.0.1:{mock_port}",
                   "mock-key", True, "openai", context_window=200000)
db.upsert_model("mock", "mock-model", 200000, True, False)
db.set_setting("active_model", {"provider_id": "mock", "model": "mock-model"})

import app as app_mod   # noqa: E402
app_server = ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8765),
                                 app_mod.Handler)
print("UI demo 已启动: http://127.0.0.1:8765   （账号 demo / demo1234）", flush=True)
print(f"token（调试用）: {token}\nTARGET: {TARGET}", flush=True)
try:
    app_server.serve_forever()
except KeyboardInterrupt:
    print("\n再见")
