"""Bounded evidence tracking, advisory only. No execution or permission decisions."""
from collections import Counter, deque
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ProgressWindow:
    def __init__(self):
        self.calls = deque(maxlen=6)
        self.warned = set()
        self.pending = None

    def observe(self, name, args, info):
        # Waiting and verification can legitimately repeat identical requests.
        if name.startswith("wait") or name in ("sleep", "browser_wait") or info.get("cancelled"):
            self.calls.clear()
            self.pending = None
            return
        signature = digest([name, args])
        evidence = {k:v for k,v in info.items() if k not in ("dur_s", "elapsed_s", "full")}
        version = digest(evidence)
        self.calls.append((signature, version, name))
        if len(self.calls) < 6:
            return
        counts = Counter(sig for sig, _, _ in self.calls)
        sequence = [sig for sig, _, _ in self.calls]
        transitions = sum(a != b for a, b in zip(sequence, sequence[1:]))
        versions = {}
        for sig, value, _ in self.calls:
            versions.setdefault(sig, set()).add(value)
        # Every request must have repeated, with unchanged evidence. A new file
        # version, new output or new search breaks the pattern immediately.
        if not 2 <= len(counts) <= 3 or transitions < 4 or min(counts.values()) < 2 or any(len(v) > 1 for v in versions.values()):
            self.pending = None
            return
        key = digest(sorted((sig, next(iter(versions[sig]))) for sig in counts))
        if key not in self.warned:
            self.warned.add(key)
            # Bounded even during very long tasks.
            if len(self.warned) > 64:
                self.warned = {key}
            self.pending = sorted(set(name for _, _, name in self.calls))


def task_state(history):
    """Keep host-recorded facts alongside, rather than inferred from, model prose."""
    state = {"latest_request":"", "modified_files":[], "checks":[], "pending":[]}
    files = []
    for message in history:
        if isinstance(message.get("_task_state"), dict):
            previous = message["_task_state"]
            state.update({k:previous[k] for k in state if k in previous})
            files = list(state["modified_files"])
        if message.get("role") == "user" and not message.get("_synthetic"):
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
            if isinstance(content, str):
                state["latest_request"] = content[:2000]
        fact = message.get("_task_evidence") or {}
        if fact.get("modified"):
            path = str(fact["modified"])[:500]
            files = [p for p in files if p != path] + [path]
        if "pending" in fact:
            state["pending"] = fact["pending"]
        if fact.get("check"):
            state["checks"] = (state["checks"] + [fact["check"]])[-6:]
    state["modified_files"] = files[-20:]
    return state


def state_note(state):
    return ("\n【宿主保留的任务事实】以下是压缩边界时的历史记录，后续用户要求和工具结果优先。"
            "未列出检查不等于检查通过；shell 成功不等于任务完成，待办仅为模型计划。\n" +
            json.dumps(state, ensure_ascii=False))
