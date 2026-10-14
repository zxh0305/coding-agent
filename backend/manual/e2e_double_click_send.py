"""E2E：发送按钮的"双击不再把自己刚发的回合停掉"

用法（需先起服务在 8099）：
    cd backend && python3 app.py 8099 &
    python3 backend/manual/e2e_double_click_send.py

背景
--------------------------------------------------------------------------------
发送按钮是"发送/停止"双角色：bind("send") 按 streaming 分流。而 performSend
一进来就同步 setStreaming(true)——首帧还没画完、按钮看着仍是"发送"时，双击的
第二次点击已经落进 stopGeneration，把刚发出的回合掐掉。用户观感是"消息发出去了
但立刻没回答"。

修法：一个 300ms 冷却窗（lastSendAt），窗内的"停止"视作同一次发送的抖动而忽略。
本用例断言两件事，缺一不可：
  1. 双击发送 → 不产生任何 /api/chat/stop（不误停）
  2. 等过冷却窗后点停止 → 确实发出 stop（没有把正常的停止功能改坏）
"""
import sys, json, urllib.request, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
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
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception:
        return {}


U = "e2edbl%d" % (int(time.time()) % 100000)
tok = api("POST", "/api/auth/register", {"username": U, "password": "probe1234"}).get("token")
assert tok, "无法取得 token"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    stops = []
    pg.on("request", lambda r: stops.append(r.url) if "/api/chat/stop" in r.url else None)

    pg.goto(BASE, wait_until="networkidle")
    pg.evaluate("(t) => localStorage.setItem('auth_token', t)", tok)
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(2000)

    # —— 断言 1：双击发送不得触发 stop ——
    pg.fill("#input", "写一篇 300 字的短文，主题是编程")
    pg.evaluate("""() => {
      const btn = document.getElementById('send');
      btn.click();
      setTimeout(() => btn.click(), 50);   // 50ms 内的第二次点击 = 抖动
    }""")
    pg.wait_for_timeout(200)
    check("双击发送后未产生 /api/chat/stop", len(stops) == 0, f"实际 {len(stops)} 次")

    # —— 断言 2：冷却窗过后，停止仍可用 ——
    pg.wait_for_timeout(600)   # 越过 300ms 冷却窗
    streaming = pg.evaluate(
        "() => document.getElementById('send').classList.contains('stop')")
    if streaming:
        pg.click("#send")
        pg.wait_for_timeout(1500)
        check("冷却后点停止确实发出 /api/chat/stop", len(stops) == 1, f"实际 {len(stops)} 次")
        check("停止后按钮复位为「发送」",
              pg.evaluate("() => document.getElementById('send').textContent") == "发送")
    else:
        # 回合已跑完（模型太快）：只报告，不判失败——断言 2 需要一个仍在跑的回合
        print("  ⚠ 回合已结束，跳过「停止仍可用」断言（换更长的任务可覆盖）")

    check("全程无 pageerror", not errs, str(errs[:2]))
    b.close()

print("\n全部通过" if not fail else f"\n失败 {fail} 项")
sys.exit(1 if fail else 0)
