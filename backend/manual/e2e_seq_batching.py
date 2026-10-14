"""E2E：SSE 续接锚点的合帧写（逐 token 事件不再逐条同步写 localStorage）

用法（需先起服务在 8099）：
    cd backend && python3 app.py 8099 &
    python3 backend/manual/e2e_seq_batching.py

背景
--------------------------------------------------------------------------------
onSSEEvent 原先对【每条】带 id 的事件执行 localStorage.setItem，而
answer_delta 是逐 token 到达的——一条长回答就是几千次同步 localStorage 写
（同步 API，阻塞主线程并触发落盘），期间点击/滚动发涩。真正需要持久化的只是
最大 seq，逐条写是纯冗余。

改为合帧写（noteSeq/flushSeq）：同帧内只写一次最大值，配 pagehide 冲刷。
本用例断言：
  1. N 条逐 token 事件 → setItem 次数远小于 N（合帧生效）
  2. 锚点最终等于最大值（没有因为合帧而少记）
  3. 注入后【立即】触发 pagehide 也能落盘（用户瞬间关页面不丢锚点）
"""
import sys, json, urllib.request
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
    except Exception as e:
        return {"__err": str(e)}


import time
U = "e2eseq%d" % (int(time.time()) % 100000)
tok = api("POST", "/api/auth/register", {"username": U, "password": "probe1234"}).get("token")
assert tok, "无法取得 token"
api("POST", "/api/sessions", {"message": "hi"}, tok)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="networkidle")
    pg.evaluate("(t) => localStorage.setItem('auth_token', t)", tok)   # 注意是 auth_token
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(2500)

    # 进入会话：让 currentSession 绑定（否则 onSSEEvent 的续接分支不执行）
    pg.evaluate("""() => {
      const items=[...document.querySelectorAll('#task-list li')]
        .filter(li=>!li.classList.contains('empty')&&!li.classList.contains('group-head'));
      if (items.length) items[0].click();
    }""")
    pg.wait_for_timeout(1500)

    N = 400
    res = pg.evaluate("""(N) => {
      let calls = 0;
      const orig = Storage.prototype.setItem;
      Storage.prototype.setItem = function(k, v) {
        if (String(k).startsWith('sse_seq_')) calls++;
        return orig.call(this, k, v);
      };
      for (let i = 1; i <= N; i++) {
        window.onSSEEvent({data: JSON.stringify({type:'answer_delta', delta:'x'}),
                           lastEventId: String(i)});
      }
      const during = calls;
      return new Promise(r => setTimeout(() => r({during, after: calls}), 800));
    }""", N)

    check(f"注入 {N} 条逐 token 事件 → setItem 次数远小于 {N}",
          res["after"] <= 5, f"实际 {res['after']}")
    check("合帧期间也没有逐条写", res["during"] <= 2, f"实际 {res['during']}")

    anchors = pg.evaluate(
        "() => Object.entries(localStorage).filter(([k]) => k.startsWith('sse_seq_'))")
    check("锚点已落盘", len(anchors) == 1, f"实际 {anchors}")
    if anchors:
        check(f"锚点等于最大值 {N}", anchors[0][1] == str(N), f"实际 {anchors[0][1]}")

    # pagehide：用户瞬间关页面，待写的值必须冲出去
    pg.evaluate("""() => {
      for (let i = 1; i <= 77; i++) {
        window.onSSEEvent({data: JSON.stringify({type:'answer_delta', delta:'y'}),
                           lastEventId: String(1000 + i)});
      }
      window.dispatchEvent(new Event('pagehide'));   // 不等 rAF，立刻冲刷
    }""")
    after = pg.evaluate(
        "() => Object.entries(localStorage).filter(([k]) => k.startsWith('sse_seq_'))")
    check("pagehide 立即冲刷（不丢最近锚点）",
          after and after[0][1] == "1077", f"实际 {after}")

    check("全程无 pageerror", not errs, str(errs[:2]))
    b.close()

print("\n全部通过" if not fail else f"\n失败 {fail} 项")
sys.exit(1 if fail else 0)
