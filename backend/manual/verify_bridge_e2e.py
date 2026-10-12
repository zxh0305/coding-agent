"""端到端验证：绑定"其他工作区"后，附件/记忆不再污染其 git 变动区，
且文件工具经桥接前缀照常可用。跑完自删（在系统临时目录）。"""
import subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from tools import ToolContext, execute_tool
from permissions import PermissionGate

tmp = Path(tempfile.mkdtemp(prefix="bridge_e2e_"))
ws = tmp / "other-project"
ws.mkdir()
db.DB_PATH = tmp / "data" / "agent_data.db"
db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db.init_db()

# 造一个"其他工作区"：git 仓库、无任何 gitignore 条目
db.create_session("sX", 1)
db.set_session_workspace("sX", str(ws))
(ws / "app.py").write_text("print('hi')\n")
def git(*a): return subprocess.run(["git", "-C", str(ws), *a], capture_output=True, text=True)
assert git("init", "-q").returncode == 0
git("add", "-A"); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
assert git("status", "--porcelain").stdout == "", "基线必须干净"

ctx = ToolContext(workspace=ws, session_id="sX")
# 与 Agent._bridge_roots 相同的装配
ctx.bridge_roots = (lambda: {
    "attachments": db.session_attachments_dir("sX").resolve(),
    ".agent-memory": __import__("memory").memory_dir(str(ws)).resolve(),
})

# 1) 上传附件（走 db 落盘路径）→ 工作区必须无变动
db.save_attachment("sX", "build.log", b"line1\nline2\n")
assert git("status", "--porcelain").stdout == "", "上传附件不该弄脏工作区"
assert (db._attachments_dir() / "sX" / "build.log").exists()

# 2) 文件工具经 attachments/ 前缀读附件
r = execute_tool("read_file", {"path": "attachments/build.log"}, ctx)
assert '"ok":true' in r.replace(" ", ""), r
assert "line1" in r, r

# 3) list_dir 看附件区 / grep 搜附件内容
r = execute_tool("list_dir", {"path": "attachments"}, ctx)
assert "build.log" in r, r
r = execute_tool("grep", {"pattern": "line2", "path": "attachments"}, ctx)
assert '"file"' in r and "line2" in r, r

# 4) 经 .agent-memory/ 前缀写记忆 → 物理落数据区分桶，工作区无变动
r = execute_tool("write_file", {"path": ".agent-memory/user-pref.md",
                                "content": "---\nname: user-pref\n---\n偏好"}, ctx)
assert '"ok":true' in r.replace(" ", ""), r
mem_bucket = db.DB_PATH.parent / "memory"
assert mem_bucket.is_dir() and list(mem_bucket.iterdir()), "记忆应落数据区分桶"
assert not (ws / ".agent-memory").exists(), "工作区不得出现记忆目录"
r = execute_tool("read_file", {"path": ".agent-memory/user-pref.md"}, ctx)
assert "偏好" in r, r
assert git("status", "--porcelain").stdout == "", "写记忆不该弄脏工作区"

# 5) 权限闸门：桥接区内的写入放行（与执行层同一套校验）
gate = PermissionGate(ws, bridge_roots_loader=lambda: ctx.bridge_roots())
v = gate.check("write_file", {"path": ".agent-memory/x.md", "content": "y"})
assert v.verb == "allow", (v.verb, v.reason)
v = gate.check("write_file", {"path": "../../etc/passwd", "content": "y"})
assert v.verb == "deny", (v.verb, v.reason)

# 6) 工作区正常读写不受影响；越界依旧被拒
r = execute_tool("write_file", {"path": "note.txt", "content": "ok"}, ctx)
assert '"ok":true' in r.replace(" ", ""), r
r = execute_tool("read_file", {"path": "../../etc/passwd"}, ctx)
assert '"ok":false' in r.replace(" ", ""), r

# 7) 遗留迁移：手工在工作区放旧布局附件+记忆 → 迁移收回、空壳清除
legacy_mem = ws / ".agent-memory"; legacy_mem.mkdir()
(legacy_mem / "old.md").write_text("旧记忆")
legacy_att = ws / ".coding-agent" / "attachments" / "sX"
legacy_att.mkdir(parents=True); (legacy_att / "old.log").write_bytes(b"old")
n = db.migrate_legacy_workspace_files("sX")
assert n == 2, n
assert (db._attachments_dir() / "sX" / "old.log").exists()
assert next(mem_bucket.iterdir()).joinpath("old.md").exists()
assert not (ws / ".coding-agent").exists() and not legacy_mem.exists()
# 工作区里唯一的变化应是第 6 步合法写入的 note.txt（桥接目录一律无痕）
assert git("status", "--porcelain").stdout == "?? note.txt\n", \
    git("status", "--porcelain").stdout

print("E2E ALL PASS")
