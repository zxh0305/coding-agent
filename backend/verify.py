"""
修改后的自动验证
================

目标：Agent 说"改完了"时，我们手里要有一份**实际执行过**的验证证据，而不是
模型的自述。system prompt 已经要求它跑测试（`system_prompt.py:38`），但那是
纪律，不是保证——模型可以跳过、可以只跑半个套件、可以说"测试通过"而根本没跑。

做法（刻意做小）：
1. **发现**验证命令：在**仓库里**查找（AGENTS.md / CLAUDE.md / package.json /
   pyproject.toml / Makefile）。只看固定文件名，不递归猜。
2. **执行**：用同一个 Executor 跑（与 run_bash 同一提供者，未来换 Docker 沙箱
   自动跟着走）。
3. **回填**：结果作为一条 system 消息塞回历史的**下一次请求前**——不是终点，
   而是 Agent 的输入。失败 → 它有机会修；通过 → 它知道确实过了。

为什么不大张旗鼓：这里不判定"任务成功"。验证失败时我们只把事实交给模型，
是否继续修由它决定（轮数上限与用户停止仍是收敛机制）。避免把"测试红"误判成
"整个回合失败"——很多任务本来就只跑一部分测试。

命令来源的安全边界：配置文件来自**工作区**（可能是不可信仓库）。所以我们
只接受白名单形状的命令（见 `_ACCEPT`），且执行前过权限闸门的
`allow_commands` 判定——工作区里的 `test = "rm -rf ~"` 不会因为"它是验证
命令"就绕过闸门。
"""

import json
import re
import shlex
import tomllib
from pathlib import Path

# 验证阶段的输出上限：失败时会回填给模型，必须够短。
MAX_REPORT_CHARS = 4000
DEFAULT_TIMEOUT = 300

# 只从工作区里这几个文件读命令。`AGENTS.md` 优先（本项目的约定）。
_AGENTS_FILES = ("AGENTS.md", "CLAUDE.md")

# ---- 命令白名单形状 -------------------------------------------------------
# 验证命令要"跑得起来且几乎无害"。不追求完备，只挡住明显不该自动跑的：
# 写盘、提权、网络下载、删库。
_ACCEPT_PROGRAMS = {
    "pytest", "py.test", "python", "python3",
    "npm", "pnpm", "yarn", "bun", "node", "deno",
    "go", "cargo", "make", "just", "mvn", "gradle", "gradlew", "mvnw",
    "ruff", "mypy", "tsc", "eslint", "flake8", "black", "isort",
    "swift", "xcodebuild", "dotnet", "ctest",
}
# 出现在【参数】位置就拒绝的词：这些不是"验证"，是副作用或提权。
# 注意这里不含 pytest/npm 这类程序名——它们作为 argv[0] 由白名单放行，
# 作为参数出现时（`python3 -c "..."` 已先被 shell 元字符规则挡掉）也不再放行。
_REJECT_TOKENS = {
    "rm", "sudo", "su", "doas", "pkexec", "chmod", "chown", "dd", "mkfs",
    "shutdown", "reboot", "curl", "wget", "ssh", "scp", "rsync",
    "git", "docker", "kubectl", "brew", "pip", "apt", "yum", "publish",
}
# 复合命令一律拒绝：`a && b`、管道、重定向、命令替换都可能把验证命令变成
# 任意命令。验证阶段要的是"一条可复现的命令"，不是 shell 脚本。
# 注意 `>=`/`<=` 这类比较符在 pytest 参数里也偶见——宁可拒绝（用户改成
# 配置文件里写），也不为少数场景放行重定向。
_SHELL_META = re.compile(r"[;&|`$<>\n]")


def _reject(cmd: str) -> bool:
    """这条命令是否拒绝自动执行。宽进严出：解析不了就拒。"""
    if not cmd or len(cmd) > 500 or _SHELL_META.search(cmd):
        return True
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return True
    if not argv:
        return True
    # 程序名允许两种形态：裸名（pytest）与相对路径（./gradlew）。绝对路径
    # 拒绝——`/tmp/x.sh` 不是这个项目的验证命令。
    prog = Path(argv[0])
    if prog.is_absolute() or ".." in prog.parts:
        return True
    if prog.name not in _ACCEPT_PROGRAMS:
        return True
    for tok in argv[1:]:
        if tok.split("=", 1)[0].strip() in _REJECT_TOKENS:
            return True
    return False


# ---- 发现 ----------------------------------------------------------------

def _from_agents_md(text: str) -> list[dict]:
    """从 AGENTS.md / CLAUDE.md 里读 `验证命令：` 段落。

    约定（尽量宽松，不逼用户改写已有文档）：

        验证命令：
        - pytest -q
        - ruff check .

    也接受单行 `验证命令：pytest -q`。
    """
    out, in_block = [], False
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^验证命令\s*[:：]\s*(.*)$", line)
        if m:
            rest = m.group(1).strip()
            if rest:
                out.append({"cmd": rest, "source": "AGENTS.md"})
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            if line.startswith(("-", "*")):
                out.append({"cmd": line[1:].strip().strip("`"), "source": "AGENTS.md"})
            elif line:
                in_block = False
    return out


def _from_package_json(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    scripts = data.get("scripts") or {}
    if "test" not in scripts:
        return []
    pm = "pnpm" if (path.parent / "pnpm-lock.yaml").exists() else (
        "yarn" if (path.parent / "yarn.lock").exists() else "npm")
    return [{"cmd": f"{pm} test", "source": "package.json"}]


def _from_pyproject(path: Path) -> list[dict]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    # 显式配置优先（用户写过 [tool.pytest.ini_options] 才认为项目用 pytest）
    tool = data.get("tool") or {}
    if "pytest" in tool:
        return [{"cmd": "python3 -m pytest -q", "source": "pyproject.toml"}]
    return []


def _from_makefile(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if re.search(r"^test\s*:", text, re.M):
        return [{"cmd": "make test", "source": "Makefile"}]
    return []


def discover(ws: Path) -> list[dict]:
    """在**【仓库根】**发现验证命令。

    只看 ws 本身，不向上找也不递归——向上找会把"父目录是另一个项目"的命令
    拿过来跑，是最容易误伤的一类错误。

    返回 [{"cmd", "source"}]，最多 3 条（太多等于跑整套 CI，太慢），
    同一条命令去重。
    """
    found: list[dict] = []
    if not ws or not ws.is_dir():
        return found
    for name in _AGENTS_FILES:
        p = ws / name
        if p.exists():
            try:
                found += _from_agents_md(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            break  # 两个都写时以 AGENTS.md 为准
    if (ws / "package.json").exists():
        found += _from_package_json(ws / "package.json")
    if (ws / "pyproject.toml").exists():
        found += _from_pyproject(ws / "pyproject.toml")
    if (ws / "Makefile").exists():
        found += _from_makefile(ws / "Makefile")

    seen, out = set(), []
    for item in found:
        cmd = item["cmd"]
        if cmd in seen or _reject(cmd):
            continue
        seen.add(cmd)
        out.append(item)
    return out[:3]


# ---- 执行 ----------------------------------------------------------------

def run(ws: Path, executor, commands: list[dict],
        timeout: int = DEFAULT_TIMEOUT) -> dict:
    """逐条跑验证命令，返回汇总。

    每条命令独立：一条失败不阻止后续（用户往往想看"测试过没过、lint 过没过"
    两个信号，而不是第一条红就停）。
    """
    results = []
    for item in commands:
        try:
            res = executor.run(item["cmd"], cwd=ws, timeout=timeout)
            results.append({
                "cmd": item["cmd"],
                "source": item.get("source", ""),
                "exit_code": res.exit_code,
                "timed_out": bool(res.timed_out),
                "output": _tail(res.stdout, res.stderr),
                "ok": (not res.timed_out) and res.exit_code == 0,
            })
        except Exception as e:  # 提供者故障不连坐其余命令
            results.append({"cmd": item["cmd"], "source": item.get("source", ""),
                            "exit_code": None, "timed_out": False,
                            "output": f"验证命令执行失败: {e}", "ok": False,
                            "error": True})
    return {"results": results, "all_ok": all(r["ok"] for r in results)}


def _tail(stdout: str, stderr: str, limit: int = MAX_REPORT_CHARS) -> str:
    """保留尾部：失败信息通常在最后几屏（traceback 底部才是真错因）。"""
    text = (stdout or "")
    if stderr and stderr.strip():
        text += ("\n[stderr]\n" if text else "") + stderr
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…（前文省略）…\n" + text[-limit:]


def as_message(report: dict) -> dict:
    """把验证报告变成一条喂回模型的 system 消息。

    明确区分"我实际跑了"与"没跑"：模型不能把这条消息误读成"用户说测试过了"。
    """
    lines = ["自动验证结果（本回合结束后由宿主执行，不是用户消息）："]
    for r in report["results"]:
        status = "PASS" if r["ok"] else ("TIMEOUT" if r["timed_out"] else "FAIL")
        lines.append(f"\n[{status}] {r['cmd']}  （来自 {r['source']}）")
        if not r["ok"] and r["output"]:
            lines.append("```\n" + r["output"] + "\n```")
    if report.get("all_ok"):
        lines.append("\n全部通过。可以在回答里说明验证命令与结果。")
    else:
        lines.append("\n有未通过的验证。如果与本次改动相关，先修复再收尾；"
                     "如果确认与本次改动无关（既有失败），在回答中明确说明，不要假装通过。")
    return {"role": "system", "content": "\n".join(lines)}
