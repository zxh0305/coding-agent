"""E2E 回归：真浏览器（Chromium）里守住两条前端不变量——

1) 首屏无 TDZ 崩溃：未登录 / 已登录加载都不得有 pageerror。守住
   resetStreamState 在 `let todoAutoOpened` 初始化前写入变量的修复
   （该 `let` 曾声明在文件后段，被 showLogin 同步调用踩进 TDZ）。
2) 无正文回合收尾健全：applyEvent 注入 turn_start → tool_call →
   tool_result → done（无任何 delta）全程不抛异常，且 done 后过程卡的
   running 类被摘（秒数停走）、streaming 态清除。

注：第 2 条的 done 分支已有 ensureLiveMsg() 保证 metaEl 非空，metaEl 空值
守卫属防御性兜底（见 app.js done 分支注释）；本用例主要防回归——若将来
收尾链（摘 running / 折叠过程卡 / 清 streaming）被某处异常中断，这里会红。
"""
import sys
import json
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
USER, PWD = "selftest_e2e", "e2e12345678"

fail = 0
def check(name, cond):
    global fail
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        fail += 1


def api(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"__status": e.code, "__body": e.read().decode()[:200]}
    except Exception as e:
        return {"__err": str(e)}


# 先注册/登录拿 token
tok = (api("POST", "/api/auth/register", {"username": USER, "password": PWD}).get("token")
       or api("POST", "/api/auth/login", {"username": USER, "password": PWD}).get("token"))
assert tok, "无法取得 token"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    # —— 断言 A：首屏 TDZ —— 未登录加载不得有 pageerror
    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_timeout(1200)
    login_errs = list(errs)
    check("未登录首屏无 pageerror（TDZ 修复）", not login_errs)
    if login_errs:
        print("     ", login_errs[:2])

    # —— 断言 B：已登录 boot —— 注入事件序列（applyEvent 直接写 #chat，不依赖会话）——
    pg.evaluate("(t) => localStorage.setItem('authToken', t)", tok)
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(1500)
    check("已登录 boot 无 pageerror", not errs)

    # 等应用就绪：applyEvent 可见、#chat 存在
    pg.wait_for_function(
        "() => typeof window.applyEvent === 'function' && !!document.getElementById('chat')",
        timeout=5000)
    pg.wait_for_timeout(300)

    inject = pg.evaluate("""() => {
      const fn = window.applyEvent;
      if (typeof fn !== 'function') return 'applyEvent 不可见';
      const steps = [];
      const seqs = [
        {type:'turn_start', mid:'m1', user_mid:'u1', started_at: Date.now()/1000, input:'跑一下测试'},
        {type:'tool_call', name:'run_bash', arguments:'{"command":"echo hi"}'},
        {type:'tool_result', name:'run_bash', result:'hi'},
      ];
      try {
        for (const e of seqs) { fn(e, null); }
        const tr = document.querySelector('.trace');
        steps.push({after:'tool_result', hasTrace: !!tr, running: tr ? tr.classList.contains('running') : null});
        fn({type:'done', mid:'m1', elapsed_s: 1.2, usage:{input:10, output:5}}, null);
        const tr2 = document.querySelector('.trace');
        steps.push({after:'done', hasTrace: !!tr2, running: tr2 ? tr2.classList.contains('running') : null});
        return {ok:true, steps};
      } catch (e) { return {ok:false, err: e.message, steps}; }
    }""")

    check("applyEvent 全程无异常（metaEl 空值修复）", inject.get("ok") is True)
    if not inject.get("ok"):
        print("     err:", inject.get("err"))
    steps = {s.get("after"): s for s in inject.get("steps", [])}
    tr1 = steps.get("tool_result", {})
    tr2 = steps.get("done", {})
    check("tool_call 后过程卡渲染且 running", tr1.get("hasTrace") and tr1.get("running") is True)
    check("done 后 running 被摘（秒数停走）", tr2.get("hasTrace") and tr2.get("running") is False)
    check("注入期间新增 pageerror 为 0", len(errs) == 0)
    if errs:
        print("     pageerror:", errs[:3])

    b.close()

print("\n全部通过" if not fail else f"\n失败 {fail} 项")
sys.exit(1 if fail else 0)
