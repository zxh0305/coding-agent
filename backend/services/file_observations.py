"""Session-local read versions and process-local file mutation serialization."""
import hashlib
import threading
from functools import wraps

_LOCKS = [threading.RLock() for _ in range(128)]


class ObservationConflict(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


class FileObservations:
    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()

    def observe(self, path, raw, start, end, total):
        version = fingerprint(raw)
        key = str(path)
        with self.lock:
            previous = self.entries.get(key, {})
            ranges = previous.get("ranges", []) if previous.get("version") == version else []
            ranges = sorted([*ranges, (start, end)])
            merged = []
            for lower, upper in ranges:
                if merged and lower <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
                else:
                    merged.append((lower, upper))
            self.entries[key] = {"version": version, "ranges": merged, "total": total}
        return version

    def require(self, path, raw, *, whole=False):
        try:
            raw.decode("utf-8")
        except UnicodeError:
            raise ObservationConflict("FS_ENCODING", "文件不是 UTF-8，不能用文本工具直接覆盖；请先确认原始编码")
        with self.lock:
            entry = self.entries.get(str(path))
        if entry is None:
            raise ObservationConflict("FS_NOT_OBSERVED", "修改前请先用 read_file 读取该文件")
        if entry["version"] != fingerprint(raw):
            raise ObservationConflict("FS_STALE_VERSION", "文件在读取后已改变，请重新读取后再修改")
        if whole and raw and entry["ranges"] != [(1, entry["total"])]:
            raise ObservationConflict("FS_NOT_FULLY_READ", "整文件覆盖前请读取完整文件；局部修改请用 apply_patch")
        return entry["version"]


def serialized_file_tool(function):
    """Use the same normalized file key for reads and writes across sessions."""
    @wraps(function)
    def wrapper(path, *args, **kwargs):
        # Tool calls use keywords; preserve positional callers by binding the signature.
        import inspect
        from code_tools import _resolve, _ws, _bridge_roots
        bound = inspect.signature(function).bind(path, *args, **kwargs)
        ctx = bound.arguments.get("ctx")
        target = _resolve(path, _ws(ctx), _bridge_roots(ctx))
        with _LOCKS[hash(str(target)) % len(_LOCKS)]:
            try:
                return function(path, *args, **kwargs)
            except ObservationConflict as e:
                import json
                return json.dumps({"ok": False, "error": str(e), "error_code": e.code,
                                   "hint": "重新读取最新文件，再基于当前内容编辑；不要原样重试旧补丁"},
                                  ensure_ascii=False)
    return wrapper
