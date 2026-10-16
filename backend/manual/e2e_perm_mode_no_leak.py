"""E2E：新任务不得静默继承上一个会话的权限模式

用法（需先起服务在 8097）：
    cd backend && python3 app.py 8097 &
    python3 backend/manual/e2e_perm_mode_no_leak.py

背景（真实踩过的坑）
--------------------------------------------------------------------------------
permMode 是全局变量，只在 loadPermMode() 里被刷成"当前会话工作区的模式"，
而该函数对新任务会早退（没有 session 可查）。newTask() 复位了一堆状态却没有
复位它，于是上一个会话的值原样留着，工具栏 chip 也照旧显示。

后果：进过一个 yolo（完全访问）会话后再点「＋ 新任务」，工具栏显示「完全访问」，
发首条消息时 performSend 的 `if (permMode !== "confirm")` 把它 POST 给新会话
——用户在【未经选择】的情况下拿到完全访问，写文件/跑命令不再逐次确认。

注意与"按工作区记忆"的区别：那是【刻意设计】（chat.py：同一工作文件夹的新任务
沿用上次选择）。本 bug 是**跨工作区**泄漏——项目绑定的 yolo 漏进无工作区的新任务，
且 chip 不刷新，用户毫无察觉。

修法：newTask() 里把 permMode 复位为 confirm 并刷新 chip。此后若用户在新任务
下拉里显式选择，那条路径会重新赋值（仍生效）；首条消息建会话后由 loadWorkspace
→ loadPermMode 读回该工作区的真实记忆。
"""
import sys, json, urllib.request, time, tempfile
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


U = "e2eperm%d" % (int(time.time()) % 100000)
tok = api("POST", "/api/auth/register", {"username": U, "password": "probe1234"}).get("token")
assert tok, "无法取得 token"
ws = tempfile.mkdtemp(prefix="e2epermws_")
s1 = api("POST", "/api/sessions", {"message": "x", "workspace": ws}, tok).get("session_id")
api("POST", "/api/sessions/%s/perm_mode" % s1, {"mode": "yolo"}, tok)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    posts = []
    pg.on("request", lambda r: posts.append(r.url) if ("perm_mode" in r.url and r.method == "POST") else None)

    pg.goto(BASE, wait_until="networkidle")
    pg.evaluate("(t) => localStorage.setItem('auth_token', t)", tok)
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(2000)

    # 进 yolo 会话：chip 应显示完全访问
    pg.evaluate("(s) => switchSession(s)", s1)
    pg.wait_for_timeout(1500)
    check("进入 yolo 会话后 permMode = yolo", pg.evaluate("() => permMode") == "yolo")
    check("chip 显示「完全访问」",
          pg.evaluate("() => document.getElementById('perm-label').textContent") == "完全访问")

    # 点新任务：必须复位
    pg.evaluate("() => newTask()")
    pg.wait_for_timeout(600)
    check("新任务后 permMode 复位为 confirm",
          pg.evaluate("() => permMode") == "confirm",
          f"实际 {pg.evaluate('() => permMode')}")
    check("chip 文案复位为「确认」",
          pg.evaluate("() => document.getElementById('perm-label').textContent") == "确认")
    check("yolo 警示样式已清除",
          not pg.evaluate("() => document.getElementById('perm-chip').classList.contains('perm-chip-warn')"))

    # 走真实发送：不得把继承值写给新会话
    posts.clear()
    pg.fill("#input", "你好")
    pg.evaluate("() => send()")
    pg.wait_for_timeout(3500)
    check("新任务发送时未写 perm_mode（未选择就不该写）", not posts,
          f"实际发出 {posts}")

    check("全程无 pageerror", not errs, str(errs[:2]))
    b.close()

print("\n全部通过" if not fail else f"\n失败 {fail} 项")
sys.exit(1 if fail else 0)
