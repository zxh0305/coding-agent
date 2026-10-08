"""压力测试（第二部分）：贴近真实前端使用形态的负载。

与 stress_test.py 互补——那个测的是"接口空跑吞吐"，这里测：
  G. 大会话历史读：单会话塞 2000 条消息，测分页读延迟随规模的变化
  H. 并发 SSE：多会话多连接同时挂，测线程/内存与建连延迟
  I. 热会话并发写：同一会话被多端同时提交（真实多标签页场景）
  J. 大会话列表页：几十个会话时的 /api/sessions 延迟
  K. 单条超大消息（长回答/长代码）读回延迟

用法：先起服务，`python3 backend/manual/stress_extended.py [BASE]`
"""
import json
import sys
import time
import uuid
import threading
import sqlite3
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
DB = "/tmp/agent-stress/data/agent_data.db"
FAILURES = []
NOTES = []


def _req(method, path, body=None, token=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000
    except Exception as e:
        return -1, str(e).encode(), (time.perf_counter() - t0) * 1000


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


def get_token():
    u = "ext_" + uuid.uuid4().hex[:8]
    st, body, _ = _req("POST", "/api/auth/register", {"username": u, "password": "stress123456"})
    if st != 200:
        print("注册失败", st, body[:200])
        sys.exit(1)
    return json.loads(body)["token"], json.loads(body)


def seed_session(uid, sid, n_msgs, msg_chars=200, answer_chars=4000):
    """直接写库造大会话（绕过回合执行，快且可控）。"""
    cols = "session_id,mid,ord,role,content"
    rows = []
    for i in range(n_msgs):
        if i % 2 == 0:
            mid = f"{sid}-u{i}"
            c = json.dumps({"role": "user", "content": "问题 " + str(i) + " " + "x" * msg_chars})
        else:
            mid = f"{sid}-a{i}"
            c = json.dumps({"role": "assistant", "content": "回答 " + str(i) + " " + "y" * answer_chars})
        rows.append((sid, mid, i, "user" if i % 2 == 0 else "assistant", c))
    conn = sqlite3.connect(DB, timeout=30)
    try:
        conn.executemany(f"INSERT INTO messages ({cols}) VALUES (?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()


def make_session(token, msg="ext"):
    st, body, _ = _req("POST", "/api/sessions", {"message": f"{msg} {uuid.uuid4().hex[:6]}"}, token=token)
    if st in (200, 201, 202):
        try:
            return json.loads(body).get("session_id")
        except Exception:
            return None
    return None


# ---------- G. 大会话分页读 ----------
def bench_big_session(token):
    print("\n=== G. 大会话历史分页读 ===")
    sid = make_session(token, "big")
    if not sid:
        FAILURES.append("G: 无法建大会话")
        return
    time.sleep(0.3)
    N = 2000
    t0 = time.perf_counter()
    seed_session(0, sid, N)
    print(f"  播种 {N} 条消息用时 {time.perf_counter()-t0:.2f}s")
    lat, errs = [], 0
    for _ in range(15):
        st, body, ms = _req("GET", f"/api/sessions/{sid}/messages?limit=100", token=token)
        lat.append(ms)
        if st != 200:
            errs += 1
    print(f"  末页 limit=100: P50 {pct(lat,50):.0f}ms P90 {pct(lat,90):.0f}ms max {max(lat):.0f}ms 错误{errs}")
    # 向上翻页多轮
    st, body, _ = _req("GET", f"/api/sessions/{sid}/messages?limit=100", token=token)
    first = json.loads(body)["messages"][0]["ord"]
    lat2, errs2 = [], 0
    before = first
    for _ in range(15):
        st, body, ms = _req("GET", f"/api/sessions/{sid}/messages?limit=100&before_ord={before}", token=token)
        lat2.append(ms)
        if st != 200:
            errs2 += 1
            break
        msgs = json.loads(body)["messages"]
        if not msgs:
            break
        before = msgs[0]["ord"]
    if lat2:
        print(f"  连续上翻 15 页: P50 {pct(lat2,50):.0f}ms P90 {pct(lat2,90):.0f}ms max {max(lat2):.0f}ms 错误{errs2}")
    # 全量加载基准（旧行为）
    st, body, ms = _req("GET", f"/api/sessions/{sid}/messages?limit=500", token=token)
    print(f"  limit=500 单次: {ms:.0f}ms body {len(body)/1024:.0f}KB")
    if pct(lat, 50) > 500:
        FAILURES.append(f"G: 大会话末页 P50 {pct(lat,50):.0f}ms 偏慢")
    NOTES.append(f"G: 大会话 {N} 条，末页读 P50 {pct(lat,50):.0f}ms")


# ---------- H. 并发 SSE ----------
def bench_sse_many(token, n_conn=60, hold=3.0):
    print(f"\n=== H. 并发 SSE {n_conn} 连接 ===")
    sids = [s for s in (make_session(token, "sse") for _ in range(6)) if s]
    if not sids:
        FAILURES.append("H: 无法建会话")
        return
    time.sleep(0.5)
    results = []
    lock = threading.Lock()
    t0 = time.perf_counter()

    def one(i):
        sid = sids[i % len(sids)]
        req = urllib.request.Request(f"{BASE}/api/sessions/{sid}/events?token={token}")
        req.add_header("Accept", "text/event-stream")
        t1 = time.perf_counter()
        ok = False
        try:
            with urllib.request.urlopen(req, timeout=hold + 10) as r:
                ok = (r.status == 200)
                r.readline()
                time.sleep(hold)  # 保持连接
        except Exception:
            ok = False
        with lock:
            results.append((ok, (time.perf_counter() - t1) * 1000))

    ths = [threading.Thread(target=one, args=(i,)) for i in range(n_conn)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    okc = sum(1 for ok, _ in results if ok)
    lats = [ms for ok, ms in results if ok]
    print(f"  建连成功 {okc}/{n_conn}  总用时 {time.perf_counter()-t0:.2f}s")
    if lats:
        print(f"  建连延迟 P50 {pct(lats,50):.0f}ms P90 {pct(lats,90):.0f}ms max {max(lats):.0f}ms")
    if okc < n_conn:
        FAILURES.append(f"H: SSE 建连 {okc}/{n_conn}")
    NOTES.append(f"H: SSE {okc}/{n_conn} 建连，max {max(lats):.0f}ms" if lats else f"H: SSE {okc}/{n_conn}")


# ---------- I. 热会话并发写 ----------
def bench_hot_write(token, conc=20, total=60):
    print(f"\n=== I. 同会话并发写 (并发{conc}, 共{total}) ===")
    sid = make_session(token, "hot")
    if not sid:
        FAILURES.append("I: 无法建会话")
        return
    time.sleep(0.3)
    lat, codes = [], {}
    lock = threading.Lock()

    def one(i):
        st, _, ms = _req("POST", f"/api/sessions/{sid}/messages", {"message": f"hot {i}"}, token=token)
        with lock:
            lat.append(ms)
            codes[st] = codes.get(st, 0) + 1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(one, range(total)))
    wall = time.perf_counter() - t0
    print(f"  用时 {wall:.2f}s  吞吐 {total/wall:.0f} req/s")
    print(f"  延迟 P50 {pct(lat,50):.0f}ms P90 {pct(lat,90):.0f}ms max {max(lat):.0f}ms")
    print(f"  状态码分布 {codes}")
    bad = sum(v for k, v in codes.items() if k not in (200, 201, 202))
    if bad:
        FAILURES.append(f"I: 热会话写 {bad}/{total} 非 2xx")
    NOTES.append(f"I: 同会话写 {conc} 并发 {total} 条，P50 {pct(lat,50):.0f}ms，{codes}")


# ---------- J. 多会话列表 ----------
def bench_session_list(token):
    print("\n=== J. 会话列表读（大量会话）===")
    for _ in range(40):
        make_session(token, "lst")
    time.sleep(0.5)
    lat, errs = [], 0
    for _ in range(20):
        st, body, ms = _req("GET", "/api/sessions", token=token)
        lat.append(ms)
        if st != 200:
            errs += 1
    print(f"  /api/sessions: P50 {pct(lat,50):.0f}ms P90 {pct(lat,90):.0f}ms max {max(lat):.0f}ms 错误{errs}")
    NOTES.append(f"J: 40+ 会话列表 P50 {pct(lat,50):.0f}ms")


# ---------- K. 超大单条消息 ----------
def bench_huge_message(token):
    print("\n=== K. 超大单条消息读回 ===")
    sid = make_session(token, "huge")
    if not sid:
        FAILURES.append("K: 无法建会话")
        return
    time.sleep(0.3)
    sid2 = make_session(token, "huge2")
    time.sleep(0.3)
    seed_session(0, sid2, 50, answer_chars=200_000)  # 每条回答 200KB → 约 5MB
    lat = []
    for _ in range(8):
        st, body, ms = _req("GET", f"/api/sessions/{sid2}/messages?limit=50", token=token)
        lat.append(ms)
    print(f"  50 条 ×200KB(约5MB) 单次读: P50 {pct(lat,50):.0f}ms max {max(lat):.0f}ms body {len(body)/1024/1024:.1f}MB")
    NOTES.append(f"K: 5MB 会话页 P50 {pct(lat,50):.0f}ms")


def main():
    token, _ = get_token()
    bench_big_session(token)
    bench_session_list(token)
    bench_hot_write(token)
    bench_sse_many(token, n_conn=60, hold=3.0)
    bench_huge_message(token)
    print("\n" + "=" * 50)
    if FAILURES:
        print("❌ 失败项:")
        for f in FAILURES:
            print("   -", f)
    else:
        print("✅ 扩展压力用例通过")
    print("ℹ 观测:")
    for n in NOTES:
        print("   -", n)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
