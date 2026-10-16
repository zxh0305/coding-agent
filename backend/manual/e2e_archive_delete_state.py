"""E2E：归档区删除弹窗的两个 pending 状态必须一起清（否则单删会删错对象）

用法（需先起服务在 8097）：
    cd backend && python3 app.py 8097 &
    python3 backend/manual/e2e_archive_delete_state.py

背景（真实踩过的坑）
--------------------------------------------------------------------------------
单删与批量删共用同一个确认弹窗，靠 pendingDeleteIds 是否为空分流：
    pendingDeleteIds ? archBatchDeleteConfirm() : confirmArchiveDelete()

而清状态的动作此前散在各调用点，漏了一条：点遮罩取消（archive-del-mask）
只调 closeArchiveDelete()，它当时只清 pendingDeleteSid，不动 pendingDeleteIds。
于是「取消批量删 → 之后单删任意一条」会走批量分支，删掉的是【那批已被取消的
id】，用户真正要删的那条安然无恙——且删除是永久不可恢复的。

修法：把两个 pending 的清空都收进 closeArchiveDelete()（所有关闭路径的必经
之地），而不是在各调用点各写一遍。

本用例断言：点遮罩取消后，两个 pending 都已清空，且后续单删不会误走批量分支。
"""
import sys, json, urllib.request, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8097"
fail = 0


def check(name, cond, extra=""):
    global fail
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        fail += 1


def api(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return {}


U = "e2earch%d" % (int(time.time()) % 100000)
tok = api("POST", "/api/auth/register", {"username": U, "password": "probe1234"}).get("token")
assert tok, "无法取得 token"
s1 = api("POST", "/api/sessions", {"message": "会话一"}, tok).get("session_id")
s2 = api("POST", "/api/sessions", {"message": "会话二"}, tok).get("session_id")
for s in (s1, s2):
    api("POST", "/api/sessions/%s/archive" % s, {}, tok)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="networkidle")
    pg.evaluate("(t) => localStorage.setItem('auth_token', t)", tok)
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(2000)

    # —— 真实 UI 路径：归档区 → 管理 → 勾选 → 删除选中 → 点遮罩取消 ——
    pg.click("#archive-btn"); pg.wait_for_timeout(500)
    pg.click("#archive-batch"); pg.wait_for_timeout(300)
    picked = pg.evaluate("""() => {
      const cb = document.querySelectorAll('#archive-list input[type=checkbox]');
      if (!cb.length) return 0;
      cb[0].checked = true; cb[0].dispatchEvent(new Event('change', {bubbles:true}));
      return cb.length;
    }""")
    check("归档区有可勾选的任务", picked > 0, f"checkbox 数量 {picked}")

    pg.evaluate("() => document.getElementById('archive-batch-del')?.click()")
    pg.wait_for_timeout(300)
    check("批量删除弹窗已出现",
          pg.evaluate("() => !document.getElementById('archive-del-mask').classList.contains('hidden')"))
    check("发起批量删时 pendingDeleteIds 已就绪",
          pg.evaluate("() => Array.isArray(pendingDeleteIds) && pendingDeleteIds.length > 0"))

    # 点遮罩取消（此前只清一半的那个路径）
    pg.evaluate("""() => {
      const m = document.getElementById('archive-del-mask');
      m.dispatchEvent(new MouseEvent('click', {bubbles:true, target:m}));
    }""")
    pg.wait_for_timeout(200)

    # —— 核心断言 ——
    ids = pg.evaluate("() => pendingDeleteIds")
    sid = pg.evaluate("() => pendingDeleteSid")
    check("遮罩取消后 pendingDeleteIds 已清空", ids is None, f"实际 {ids}")
    check("遮罩取消后 pendingDeleteSid 已清空", sid is None, f"实际 {sid}")
    check("弹窗已关闭",
          pg.evaluate("() => document.getElementById('archive-del-mask').classList.contains('hidden')"))

    # 后续单删不得被误判成批量
    routes = pg.evaluate("""([s2]) => {
      pendingDeleteSid = s2;                     // 模拟：用户对 s2 发起单删
      const toBatch = !!pendingDeleteIds;
      return { toBatch, wouldDelete: toBatch ? pendingDeleteIds : [pendingDeleteSid], want: s2 };
    }""", [s2])
    check("后续单删走单删分支（不再误删上一批）",
          not routes["toBatch"] and routes["wouldDelete"] == [s2],
          f"会删 {routes['wouldDelete']}，用户想删 [{s2}]")

    check("全程无 pageerror", not errs, str(errs[:2]))
    b.close()

print("\n全部通过" if not fail else f"\n失败 {fail} 项")
sys.exit(1 if fail else 0)
