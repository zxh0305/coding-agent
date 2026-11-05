"""
回合级变更快照
==============

一次回合里 Agent 可能连续改十几个文件，最后测试失败——用户想知道"到底改了
什么"、并能把它退回去。本模块用 git 提供这两件事，且**不干扰用户自己的工作区**：

1. 回合开始：`git stash create` 生成一个悬挂提交（dangling commit），记下它的
   hash。它把"当前工作树（含未提交改动）"完整拍成一张快照，**但不移动任何
   ref、不碰索引与工作树**——用户手头未提交的代码仍在原处，`git status` 不变。
2. 回合结束：`git diff <快照> --` 得到 Agent 这一回合的完整改动（不依赖
   `git add`，也不依赖"工作区原本是干净的"这个不成立的假设）。
3. 回退：`git checkout <快照> -- <文件>` 只把指定文件还原到快照内容。

为什么不用 `git reset --hard` / `git clean -fd`：那会把用户**自己**的未提交
改动一起抹掉。快照是只读的参照点，回退粒度到文件、由调用方决定范围。

非 git 工作区：全部返回 None / available=False，调用方静默降级——不给
非 git 用户塞一个 git init。
"""

import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

GIT_TIMEOUT = 20
MAX_DIFF_CHARS = 60000   # 单个回合的 diff 上限：超长截断并明示（别撑爆上下文）


class SnapshotError(Exception):
    """git 不可用 / 目录不是仓库 / 命令失败。"""


def _git(ws: Path, *args: str, env: dict | None = None) -> str:
    """在 ws 里跑一条 git 命令，返回 stdout；失败抛 SnapshotError。

    参数列表传入（不用 shell=True）：路径与 ref 永远不会被解释成 shell 语法。
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(ws), env=env,
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
            errors="replace",
        )
    except FileNotFoundError:
        raise SnapshotError("系统里没有找到 git 命令")
    except subprocess.TimeoutExpired:
        raise SnapshotError(f"git 命令超过 {GIT_TIMEOUT} 秒未返回")
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise SnapshotError(lines[-1] if lines else f"git 退出码 {proc.returncode}")
    return proc.stdout


def is_repo(ws: Path) -> bool:
    try:
        return _git(ws, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except SnapshotError:
        return False


def create(ws: Path) -> str | None:
    """给当前工作树拍一张快照，返回 base 提交；非仓库或空仓库返回 None。

    `git stash create` 有未提交改动时输出一个提交 hash，无改动时输出空串
    （此时工作树 == HEAD，直接用 HEAD 当基准）。两种都**不动**索引、工作树
    与任何 ref。
    """
    if not is_repo(ws):
        return None
    try:
        out = _git(ws, "stash", "create").strip()
        if out:
            return out
        return _git(ws, "rev-parse", "HEAD").strip() or None
    except SnapshotError:
        # 索引损坏、锁残留、空仓库（无 HEAD）：快照失败不该让回合失败，
        # 只是失去本次的 diff/回退能力
        return None


def _changed_fingerprint(ws, base, env=None, max_bytes=32 * 1024 * 1024):
    digest, total = hashlib.sha256(), 0
    paths = _git(ws, "diff", "--name-only", "-z", base, "--", env=env).split("\0")
    for name in sorted(path for path in paths if path):
        path = ws / name
        digest.update(name.encode() + b"\0")
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif not path.exists():
            digest.update(b"<deleted>")
        elif path.is_file():
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SnapshotError("本轮改动超过版本读取上限，无法完整确认检查范围")
                    digest.update(chunk)
        else:
            raise SnapshotError("本轮改动包含子模块或特殊文件，无法完整确认检查范围")
        digest.update(b"\0")
    return digest.hexdigest()


def diff(ws: Path, base: str, max_chars: int = MAX_DIFF_CHARS) -> dict:
    """base 快照 → 当前工作树的完整改动，包含未跟踪的新文件。

    未跟踪文件经**临时索引**纳入（GIT_INDEX_FILE 指向 mkstemp 出来的文件，
    read-tree 到 base 后 `add -N` 再 diff）：用户真实的 .git/index 一个字节
    都不动，也不需要 `git add` 用户的文件。
    """
    # -z：NUL 分隔且**不加引号**。普通模式下含特殊字符（这里连 `.py` 都会）
    # 的路径会被 core.quotePath 包成 "new.py"，按行切再截前两字符就把引号
    # 一起切掉了，pathspec 直接不匹配（实测过）。
    untracked = [p for p in _git(ws, "ls-files", "-z", "--others",
                                 "--exclude-standard").split("\0") if p]
    if untracked:
        fd, idx = tempfile.mkstemp(prefix="agent-snapshot-")
        os.close(fd)
        try:
            env = dict(os.environ, GIT_INDEX_FILE=idx)
            _git(ws, "read-tree", base, env=env)
            _git(ws, "add", "-N", "--", *untracked, env=env)
            text = _git(ws, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", base, "--", env=env)
            version = _changed_fingerprint(ws, base, env)
            stat_text = _git(ws, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--stat", base, "--", env=env).strip()
        finally:
            try:
                os.unlink(idx)
            except OSError:
                pass
    else:
        text = _git(ws, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", base, "--")
        version = _changed_fingerprint(ws, base)
        stat_text = _git(ws, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--stat", base, "--").strip()

    return {"base": base, "stat": stat_text, "diff": text[:max_chars],
            "fingerprint": version,
            "truncated": len(text) > max_chars}


def restore(ws: Path, base: str, paths: list[str]) -> list[str]:
    """把指定路径还原到 base 快照的内容；返回实际还原的路径。

    只动这些路径的工作树与索引内容，不 reset 其它文件。路径先挡一层越界：
    checkout 的 pathspec、`.git/` 内部文件、`..` 逃逸一律拒绝（即使快照 base
    是调用方传的，也不能假设它可信）。
    """
    safe = []
    for p in paths:
        parts = Path(p).parts if p else ()
        if not p or Path(p).is_absolute() or ".." in parts or ".git" in parts:
            raise SnapshotError(f"非法回退路径：{p}")
        safe.append(p)
    if not safe:
        return []
    _git(ws, "checkout", base, "--", *safe)
    return safe


def working_tree_fingerprint(ws: Path, max_files=4096, max_bytes=32 * 1024 * 1024) -> str:
    """Bounded fallback for non-Git projects, including changes made through shell."""
    from code_tools import IGNORED_DIRS
    digest = hashlib.sha256()
    files, total = 0, 0
    def failed_walk(error):
        raise SnapshotError("无法遍历项目目录：" + str(error))
    for directory, dirs, names in os.walk(ws, followlinks=False, onerror=failed_walk):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS)
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise SnapshotError("非 Git 项目存在目录链接，无法完整确认检查范围")
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink():
                raise SnapshotError("非 Git 项目存在文件链接，无法完整确认检查范围")
            if not stat.S_ISREG(path.stat().st_mode):
                raise SnapshotError("项目存在特殊文件，无法完整确认检查范围")
            files += 1
            if files > max_files:
                raise SnapshotError("非 Git 项目超过检查版本文件数量上限")
            digest.update(str(path.relative_to(ws)).encode() + b"\0")
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SnapshotError("非 Git 项目超过检查版本读取上限")
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()
