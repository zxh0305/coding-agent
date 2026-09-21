"""
apply_patch 一次成功率真机评估（改造前后对比用）
==================================================

评估问题：让 agent 完成「给某函数加一个参数并修好所有调用点」，统计——
  1. 是否先读再改（首个 apply_patch 之前是否出现过 read_file/list_dir/grep）；
  2. apply_patch 一次成功率（该任务【第一次】apply_patch 即成功 / 任务数）；
  3. 锚定失败自查：apply_patch 失败后是否先 read_file/grep 复读再重试；
  4. 最终任务成败：agent 结束后独立运行工作区的 check.py，退出码 0 = PASS。

任务族：5 个同构变体（不同函数/参数/调用点数量），每个变体生成一个全新
临时工作区：1 个定义模块 + 2~3 个消费模块（调用点必须把新参数透传，
check.py 用显式传参断言行为——默认值不改调用点也能过，但那不算"修好"）。
check.py 是判分的唯一真相：模型自己说完成不算数。

用法（在同一份任务集上对比两份代码）：
  cd backend && python3 manual/eval_apply_patch_success.py --label after
  # 旧代码：git worktree add /tmp/before HEAD，拷贝 .env 与本脚本后
  cd /tmp/before/backend && python3 manual/eval_apply_patch_success.py --label before

只依赖 agent / llm_client / 标准库，新旧代码都能跑（接口一致）。
真实调用 LLM（读 CWD 的 .env），一次完整评估 = 5 个任务 × 若干轮请求。
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend 目录

from agent import Agent  # noqa: E402
from llm_client import create_llm_client  # noqa: E402

# ---------------------------------------------------------------------------
# 5 个同构任务变体：加一个参数 + 修所有调用点 + check.py 判分
# 每个变体 = (任务说明, {文件名: 文件内容})；调用点都要求把新参数透传，
# check.py 用显式实参断言——不透传的调用点必然 assert 失败。
# ---------------------------------------------------------------------------

VARIANTS = [
    {
        "name": "currency-prefix",
        "task": ("任务：给 prices.py 里的 format_price(price) 函数增加一个参数 currency"
                 "（默认值 \"CNY\"），函数返回「货币代码 + 两位小数价格」（如 format_price(3.5, \"USD\")"
                 " == \"USD3.50\"）。把项目中所有调用 format_price 的地方同步修改：各消费函数新增同名"
                 " currency 形参并透传给 format_price。全部改完后运行 python3 check.py 验证，"
                 "通过后总结改动。"),
        "files": {
            "prices.py": 'def format_price(price):\n    return f"{price:.2f}"\n',
            "report.py": ('from prices import format_price\n\n'
                          'def render(price):\n'
                          '    return "价格: " + format_price(price)\n'),
            "cart.py": ('from prices import format_price\n\n'
                        'def total(items):\n'
                        '    return "合计 " + format_price(sum(items))\n'),
            "check.py": (
                'from report import render\n'
                'from cart import total\n'
                'from prices import format_price\n'
                'assert format_price(3.5) == "CNY3.50", format_price(3.5)\n'
                'assert format_price(3.5, currency="USD") == "USD3.50"\n'
                'assert render(9.0, currency="JPY") == "价格: JPY9.00", render(9.0, currency="JPY")\n'
                'assert total([1, 2], currency="EUR") == "合计 EUR3.00"\n'
                'print("check ok")\n'),
        },
    },
    {
        "name": "scale-precision",
        "task": ("任务：给 sensor_lib.py 里的 scale(value, factor) 函数增加一个参数 precision"
                 "（默认值 2），返回值改为按 precision 位小数四舍五入（round(x, precision)）。"
                 "把所有调用 scale 的地方同步修改：各消费函数新增同名 precision 形参并透传。"
                 "全部改完后运行 python3 check.py 验证，通过后总结改动。"),
        "files": {
            "sensor_lib.py": ('def scale(value, factor):\n    return value * factor\n'),
            "dashboard.py": ('from sensor_lib import scale\n\n'
                             'def panel(v):\n'
                             '    return scale(v, 2.0)\n'),
            "exporter.py": ('from sensor_lib import scale\n\n'
                            'def dump(v):\n'
                            '    return {"v": scale(v, 0.5)}\n'),
            "check.py": (
                'from sensor_lib import scale\n'
                'from dashboard import panel\n'
                'from exporter import dump\n'
                'assert scale(3, 2.0) == 6.0\n'
                'assert scale(3.14159, 1.0, precision=4) == 3.1416\n'
                'assert panel(1.111, precision=1) == 2.2, panel(1.111, precision=1)\n'
                'assert dump(1.0, precision=3)["v"] == 0.5\n'
                'print("check ok")\n'),
        },
    },
    {
        "name": "log-level-default",
        "task": ("任务：给 logfmt.py 里的 fmt(level, message) 函数增加一个参数 app"
                 "（默认值 \"web\"），返回值改为 \"app|LEVEL|message\" 格式。"
                 "把所有调用 fmt 的地方同步修改：各消费函数新增同名 app 形参并透传。"
                 "全部改完后运行 python3 check.py 验证，通过后总结改动。"),
        "files": {
            "logfmt.py": ('def fmt(level, message):\n    return f"{level}|{message}"\n'),
            "api.py": ('from logfmt import fmt\n\n'
                       'def req(msg):\n'
                       '    return fmt("INFO", msg)\n'),
            "worker.py": ('from logfmt import fmt\n\n'
                          'def job(msg):\n'
                          '    return fmt("WARN", msg)\n'),
            "check.py": (
                'from logfmt import fmt\n'
                'from api import req\n'
                'from worker import job\n'
                'assert fmt("INFO", "x") == "web|INFO|x", fmt("INFO", "x")\n'
                'assert fmt("INFO", "x", app="pay") == "pay|INFO|x"\n'
                'assert req("hi", app="admin") == "admin|INFO|hi"\n'
                'assert job("slow", app="cron") == "cron|WARN|slow"\n'
                'print("check ok")\n'),
        },
    },
    {
        "name": "area-unit",
        "task": ("任务：给 geometry.py 里的 area(w, h) 函数增加一个参数 unit"
                 "（默认值 \"m2\"），返回值改为 \"<数值><unit>\"（如 area(2, 3) == \"6m2\"）。"
                 "把所有调用 area 的地方同步修改：各消费函数新增同名 unit 形参并透传。"
                 "全部改完后运行 python3 check.py 验证，通过后总结改动。"),
        "files": {
            "geometry.py": ('def area(w, h):\n    return w * h\n'),
            "plot.py": ('from geometry import area\n\n'
                        'def describe(w, h):\n'
                        '    return f"地块 {area(w, h)}"\n'),
            "survey.py": ('from geometry import area\n\n'
                          'def list_areas(pairs):\n'
                          '    return ", ".join(str(area(w, h)) for w, h in pairs)\n'),
            "check.py": (
                'from geometry import area\n'
                'from plot import describe\n'
                'from survey import list_areas\n'
                'assert area(2, 3) == "6m2", area(2, 3)\n'
                'assert area(2, 3, unit="cm2") == "6cm2"\n'
                'assert describe(1, 1, unit="km2") == "地块 1km2"\n'
                'assert list_areas([(1, 1), (2, 2)], unit="dm2") == "1dm2, 4dm2"\n'
                'print("check ok")\n'),
        },
    },
    {
        "name": "truncate-ellipsis",
        "task": ("任务：给 textutil.py 里的 clip(s, n) 函数增加一个参数 mark"
                 "（默认值 \"...\"），当 s 超过 n 个字符时返回 s[:n] + mark，否则原样返回。"
                 "把所有调用 clip 的地方同步修改：各消费函数新增同名 mark 形参并透传。"
                 "全部改完后运行 python3 check.py 验证，通过后总结改动。"),
        "files": {
            "textutil.py": ('def clip(s, n):\n    return s[:n]\n'),
            "feed.py": ('from textutil import clip\n\n'
                        'def headline(t):\n'
                        '    return clip(t, 10)\n'),
            "cli.py": ('from textutil import clip\n\n'
                       'def echo(t):\n'
                       '    return clip(t, 5)\n'),
            "check.py": (
                'from textutil import clip\n'
                'from feed import headline\n'
                'from cli import echo\n'
                'assert clip("abcdefgh", 3) == "abc...", clip("abcdefgh", 3)\n'
                'assert clip("ab", 3) == "ab"\n'
                'assert clip("abcdefgh", 3, mark="…") == "abc…"\n'
                'assert headline("x" * 20, mark=">>") == "x" * 10 + ">>"\n'
                'assert echo("abcdef", mark="!") == "abcde!"\n'
                'print("check ok")\n'),
        },
    },
]


def build_workspace(root: Path, files: dict) -> Path:
    # 每个变体独立子目录：变体间绝不共享工作区（否则第二个起 mkdir 直接炸）
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (ws / name).write_text(content, encoding="utf-8")
    return ws


def run_one(variant: dict, workdir: Path) -> dict:
    """跑一个变体：返回该任务的指标。"""
    ws = build_workspace(workdir, variant["files"])
    llm = create_llm_client()
    agent = Agent(llm=llm, verbose=False, workspace=str(ws))

    calls, results = [], []
    for kind, payload in agent.run(variant["task"]):
        if kind == "tool_call":
            calls.append((payload["name"], payload["arguments"]))
        elif kind == "tool_result":
            results.append(payload["result"])
    assert len(calls) == len(results), "回填顺序不变式：tool_call 与 tool_result 一一对应"

    # 指标①②③：只看 apply_patch 的事件序列（顺序 = 请求顺序 = 结果顺序）
    patch_idx = [i for i, (n, _) in enumerate(calls) if n == "apply_patch"]
    read_idx = [i for i, (n, _) in enumerate(calls) if n in ("read_file", "list_dir", "grep")]
    first_patch_ok = None
    if patch_idx:
        first_patch_ok = json.loads(results[patch_idx[0]]).get("ok") is True
    failed = [i for i in patch_idx if json.loads(results[i]).get("ok") is False]
    # 锚定失败自查：每个失败之后、下一次 apply_patch 之前，有没有先复读
    reread_after_fail = True
    for k, fi in enumerate(failed):
        nxt = [j for j in patch_idx if j > fi]
        nxt = nxt[0] if nxt else len(calls)
        window = [i for i in read_idx if fi < i < nxt]
        if k < len(failed) - 1 or nxt < len(calls):  # 后面还打算再改 → 必须先复读
            if not window:
                reread_after_fail = False

    # 指标④：check.py 独立判分（不信模型的总结）
    check = subprocess.run([sys.executable, "check.py"], cwd=ws,
                           capture_output=True, text=True, timeout=30)
    passed = check.returncode == 0

    return {
        "variant": variant["name"],
        "passed": passed,
        "first_patch_ok": first_patch_ok,
        "apply_patch_calls": len(patch_idx),
        "failed_patch_calls": len(failed),
        "read_before_patch": bool(read_idx and patch_idx and read_idx[0] < patch_idx[0]),
        "reread_after_fail": reread_after_fail if failed else None,
        "check_fail_output": (check.stdout + check.stderr)[-300:] if not passed else "",
        "rounds": sum(1 for c in calls),
        "total_tool_calls": len(calls),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run", help="报告标签（如 before / after）")
    parser.add_argument("--variants", type=int, default=len(VARIANTS))
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix=f"eval_patch_{args.label}_"))
    print(f"[{args.label}] 工作目录: {root}")
    rows = []
    for v in VARIANTS[: args.variants]:
        t0 = time.time()
        try:
            # 每个变体一个独立工作根：变体间零共享
            row = run_one(v, root / v["name"])
        except Exception as e:  # 单任务崩溃不拖垮整批：记为失败继续
            row = {"variant": v["name"], "passed": False, "first_patch_ok": None,
                   "apply_patch_calls": 0, "failed_patch_calls": 0,
                   "read_before_patch": False, "reread_after_fail": None,
                   "check_fail_output": f"任务异常: {e}", "rounds": 0, "total_tool_calls": 0}
        row["seconds"] = round(time.time() - t0, 1)
        rows.append(row)
        print(f"  [{row['variant']}] pass={row['passed']} first_patch_ok={row['first_patch_ok']} "
              f"patch={row['apply_patch_calls']}(fail={row['failed_patch_calls']}) "
              f"read_first={row['read_before_patch']} {row['seconds']}s")
        if not row["passed"]:
            print(f"    check 输出: {row['check_fail_output'][-200:]}")

    n = len(rows)
    summary = {
        "label": args.label,
        "tasks": n,
        "pass_rate": sum(r["passed"] for r in rows) / n,
        "first_try_success_rate": sum(1 for r in rows if r["first_patch_ok"]) / n,
        "total_apply_patch_calls": sum(r["apply_patch_calls"] for r in rows),
        "total_failed_patch_calls": sum(r["failed_patch_calls"] for r in rows),
        "read_before_patch_rate": sum(r["read_before_patch"] for r in rows) / n,
    }
    print("\n===== 汇总 =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    out = Path(__file__).resolve().parent / f"eval_patch_{args.label}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"明细已写入 {out}")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
