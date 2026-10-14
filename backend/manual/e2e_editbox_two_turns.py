"""E2E 回归：连做两轮「改文件」的任务，第二轮的改动不得落进第一轮的修改区。

用法（需先起服务在 8099 端口）：
    cd backend && python3 app.py 8099 &
    python3 backend/manual/e2e_editbox_two_turns.py

背景（真实踩过的坑）
--------------------------------------------------------------------------------
editBox 是 .edit-box（修改区）的模块级引用。procBox / noteBox / thinkBox /
subagentBox 都在 turn_start 与 resetStreamState 里置 null，**唯独 editBox
从不置空**（唯一赋值在 editBoxEl 的接管逻辑里）。

而回合结束后旧 .trace 并不从 DOM 移除（done 只摘 running、改 summary），
于是 editBox.isConnected 仍为真，editBoxEl() 的

    if (editBox && editBox.isConnected) return editBox;

会把【上一回合】的修改区原样交回。结果：第二回合的 write_file/apply_patch
卡片全被 append 进第一回合的过程卡里，第二回合自己的卡片空无一物。

用户观感就是"这块内容跑到别的块里了 / 内容看着翻倍"，且【刷新页面即恢复】
——刷新会重建整个 DOM，editBox 归零。

本用例用两个回合、每回合一次 write_file，断言：
- 每回合各自有一张 .trace（不是共用一张）
- 每张 .trace 里的 .edit-box 恰好只有【本回合】那一条改动
- 合计改动条数 == 2（既不丢也不重复）
"""
import sys
import json
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
USER, PWD = "selftest_editbox", "e2e12345678"

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
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"__status": e.code, "__body": e.read().decode()[:200]}
    except Exception as e:
        return {"__err": str(e)}


tok = (api("POST", "/api/auth/register", {"username": USER, "password": PWD}).get("token")
       or api("POST", "/api/auth/login", {"username": USER, "password": PWD}).get("token"))
assert tok, "无法取得 token"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    pg.goto(BASE, wait_until="networkidle")
    pg.evaluate("(t) => localStorage.setItem('authToken', t)", tok)
    pg.reload(wait_until="networkidle")
    pg.wait_for_function(
        "() => typeof window.applyEvent === 'function' && !!document.getElementById('chat')",
        timeout=8000)
    pg.wait_for_timeout(300)
    check("boot 无 pageerror", not errs)

    # 注入两个回合，每回合一次 write_file（走真实 applyEvent 路径）
    res = pg.evaluate("""() => {
      const fn = window.applyEvent;
      const file = (n) => JSON.stringify({path:'/tmp/f'+n+'.txt', content:'x'});
      const turn = (n, mid) => ([
        {type:'turn_start', mid, user_mid:'u'+n, started_at: Date.now()/1000, input:'改个文件'+n},
        {type:'tool_call', name:'write_file', arguments: file(n)},
        {type:'tool_result', name:'write_file', result:'ok'},
        {type:'done', mid, elapsed_s: 0.5, usage:{input:10, output:5}},
      ]);
      try {
        for (const e of turn(1,'m1')) fn(e, null);
        for (const e of turn(2,'m2')) fn(e, null);
      } catch (e) { return {ok:false, err:e.message}; }

      // 量一量：每张 .trace 里各有几条 .edit-box 条目、以及是否只装本回合的。
      // 一次改文件在修改区里是【两行】：`tl write card`（调用行）+ `tl result`
      // （结果行——appendTrace 注释明确"结果行跟随其调用行的归属"）。所以按
      // 调用行（.tl.card）计数才等于"改了几次文件"，.tl 总数是它的两倍。
      const traces = Array.from(document.querySelectorAll('.trace'));
      const per = traces.map(tr => {
        const eb = tr.querySelector('.edit-box');
        const calls = eb ? eb.querySelectorAll('.tl.card').length : 0;
        const all = eb ? eb.querySelectorAll('.tl').length : 0;
        const txt = eb ? eb.textContent : '';
        return {calls, all, txt};
      });
      const totalCalls = document.querySelectorAll('.edit-box .tl.card').length;
      return {ok:true, n:traces.length, per, totalCalls,
              editBoxCount: document.querySelectorAll('.edit-box').length};
    }""")

    if not res.get("ok"):
        check("两回合注入无异常", False, res.get("err"))
    else:
        check("两回合注入无异常", True)
        traces, per = res["n"], res["per"]

        # 核心断言：不能共用同一张 trace（说明第二回合正确新建了卡）
        check("两个回合各自一张 .trace", traces == 2, f"实际 {traces}")
        check("每张 .trace 各一个 .edit-box", res["editBoxCount"] == 2,
              f"实际 {res['editBoxCount']}")

        # 核心断言：每回合的修改区只装本回合那一次改动
        # （bug 时：第一张卡装 2 次、第二张卡 0 次，且共用同一个 .edit-box）
        counts = [x["calls"] for x in per]
        check("每回合修改区各 1 次改动", counts == [1, 1], f"实际分布 {counts}")
        check("改动总数 == 2（不丢不重）", res["totalCalls"] == 2, f"实际 {res['totalCalls']}")

        # 归属正确性：第一张卡提 f1、第二张卡提 f2（没有串位）
        if traces == 2 and all(x["txt"] for x in per):
            check("第一回合的改动留在第一回合卡内", "f1.txt" in per[0]["txt"],
                  per[0]["txt"][:80])
            check("第二回合的改动留在第二回合卡内", "f2.txt" in per[1]["txt"],
                  per[1]["txt"][:80])

    check("注入期间无 pageerror", len(errs) == 0, str(errs[:2]))
    b.close()

print("\n全部通过" if not fail else f"\n失败 {fail} 项")
sys.exit(1 if fail else 0)
