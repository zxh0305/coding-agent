"""Bounded completion policy. All commands use the Agent's normal tool/permission path."""
import hashlib
import json
import shlex
import time
import uuid
from pathlib import Path

import snapshot
import verify
from services.project_environment import load_config


def commands_for(workspace, profile):
    root = Path(workspace).resolve()
    config, errors = load_config(root)
    if errors:
        raise ValueError("；".join(errors))
    repair = config.get("repair", {})
    if not isinstance(repair, dict):
        raise ValueError("repair 必须是对象")
    enabled = repair.get("enabled", True)
    attempts = repair.get("max_attempts", 2)
    total = repair.get("total_timeout_seconds", 600)
    if not isinstance(enabled, bool) or type(attempts) is not int or not 0 <= attempts <= 5:
        raise ValueError("修复次数须在0至5之间，enabled 须为布尔值")
    if type(total) is not int or not 1 <= total <= 1800:
        raise ValueError("验证总时长须在1至1800秒之间")
    if "checks" not in config:
        items = [{**item, "id":str(index), "cwd":".", "level":"unspecified",
                  "timeout_seconds":300, "argv":shlex.split(item["cmd"])}
                 for index, item in enumerate(verify.discover(root))]
    else:
        raw = config["checks"]
        if not isinstance(raw, list) or len(raw) > 6:
            raise ValueError("checks 须为数组，最多6项")
        items, seen = [], set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("检查配置必须是对象")
            key = item.get("id")
            argv = item.get("argv")
            cwd = item.get("cwd", ".")
            timeout = item.get("timeout_seconds", 300)
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError("检查 id 不能为空或重复")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
                raise ValueError("检查 argv 须为非空字符串数组")
            if not isinstance(cwd, str) or Path(cwd).is_absolute() or not (root / cwd).resolve().is_relative_to(root):
                raise ValueError("检查 cwd 必须在项目内")
            if type(timeout) is not int or not 1 <= timeout <= 600:
                raise ValueError("检查超时须在1至600秒之间")
            if not isinstance(item.get("level", "unspecified"), str):
                raise ValueError("检查 level 必须是字符串")
            seen.add(key)
            items.append({**item, "source":".agents/project.json", "timeout_seconds":timeout,
                          "cwd":cwd, "level":item.get("level", "unspecified")})
    selected = (profile or {}).get("selected")
    result = []
    for item in items:
        argv = list(item["argv"])
        if any("{" in arg or "}" in arg for arg in argv if arg != "{python}"):
            raise ValueError("只支持 {python} 解释器占位符")
        if "{python}" in argv:
            if not selected:
                raise ValueError("检查需要项目 Python，请先选择解释器")
            argv = [selected["path"] if arg == "{python}" else arg for arg in argv]
        elif selected and argv[0] in ("python", "python3", "pytest", "py.test"):
            argv = [selected["path"], *argv[1:]] if argv[0] in ("python", "python3") else [selected["path"], "-m", "pytest", *argv[1:]]
        command = shlex.join(argv)
        result.append({**item, "argv":argv, "cmd":command})
    return result, {"enabled":enabled, "max_attempts":attempts, "total":total}, config


class VerificationPolicy:
    def __init__(self, workspace, base=None, *, store=None, sid=None, mid=None):
        self.root = Path(workspace).resolve()
        self.base = base
        self.store, self.sid, self.mid = store, sid, mid
        self.assessment = "not_applicable"
        self.reason = ""
        self.results = []
        self.repairs = 0
        self.batches = 0
        self.elapsed = 0.0
        self.checked_fingerprint = None
        self.sent_terminal_feedback = False
        self.initial_tree = None
        self.phase = "working"
        self.baseline_error = None

    def _phase(self, phase):
        self.phase = phase
        if self.store:
            self.store.set_assessment(self.sid, self.mid, phase, self.assessment, self.reason, self.repairs)
        return ("turn_phase", {"user_mid":self.mid, "phase":phase,
                               "assessment":self.assessment, "reason":self.reason, "attempt":self.repairs})

    def _state(self, agent):
        if self.base:
            diff = snapshot.diff(self.root, self.base)
            changed = bool(diff.get("diff") or diff.get("stat"))
            # Older injected snapshot providers may not supply fingerprint.
            version = diff.get("fingerprint") or hashlib.sha256(diff.get("diff", "").encode()).hexdigest()
            changed = version != self.initial_tree
        else:
            version = snapshot.working_tree_fingerprint(self.root)
            changed = version != self.initial_tree
        if not changed and not self.checked_fingerprint:
            return False, version
        config, errors = load_config(self.root)
        if errors:
            raise ValueError("；".join(errors))
        profile = agent.environment_profile or {}
        environment = json.dumps(profile, sort_keys=True)
        runtime = (profile.get("selected") or {}).get("path")
        if runtime:
            stat = Path(runtime).stat()
            environment += str((stat.st_size, stat.st_mtime_ns))
        version = hashlib.sha256((version + json.dumps(config, sort_keys=True) + environment).encode()).hexdigest()
        return changed, version

    def start(self, agent):
        try:
            if self.base:
                initial = snapshot.diff(self.root, self.base)
                self.initial_tree = initial.get("fingerprint") or hashlib.sha256(initial.get("diff", "").encode()).hexdigest()
            else:
                self.initial_tree = snapshot.working_tree_fingerprint(self.root)
        except (OSError, snapshot.SnapshotError) as e:
            self.baseline_error = str(e)

    def _save_result(self, check_id, item, result, status, output, output_ref=None):
        detail = {**item, **result, "ok":status == "passed", "status":status,
                  "output":output[-4000:]}
        if self.store:
            self.store.finish_check(self.sid, check_id, status, detail, output,
                                    output_ref=output_ref)
        self.results.append({**detail, "check_id":check_id})
        return ("check_update", {"user_mid":self.mid, "check_id":check_id, **detail})

    def before_answer(self, agent, candidate, *, can_repair=True):
        if agent.cancel_event.is_set():
            self.assessment, self.reason = "cancelled", "用户已停止"
            yield self._phase("finalizing")
            return None
        try:
            if self.baseline_error:
                if not getattr(agent, "_mutation_possible", False):
                    return None
                raise ValueError(self.baseline_error)
            changed, current = self._state(agent)
        except (OSError, ValueError, snapshot.SnapshotError) as e:
            self.assessment, self.reason = "blocked", "无法确认验证范围：" + str(e)
            yield self._phase("finalizing")
            return self._terminal_feedback()
        if not changed:
            self.assessment, self.reason = "not_applicable", ""
            return None
        if self.checked_fingerprint == current:
            return None if self.sent_terminal_feedback else self._terminal_feedback()
        if self.sent_terminal_feedback:
            self.assessment, self.reason = "insufficient", "最终回复前代码或环境已变化，原检查结果已过期"
            yield self._phase("finalizing")
            return None
        try:
            commands, limits, config = commands_for(self.root, agent.environment_profile)
        except (ValueError, OSError) as e:
            self.assessment, self.reason = "blocked", str(e)
            yield self._phase("finalizing")
            return self._terminal_feedback()
        if not commands:
            self.assessment, self.reason = "insufficient", "未配置可执行的项目检查"
            yield self._phase("finalizing")
            return self._terminal_feedback()
        if self.elapsed >= limits["total"] or self.batches >= 6:
            self.assessment, self.reason = "insufficient", "已达到本轮检查总预算"
            yield self._phase("finalizing")
            return self._terminal_feedback()
        self.batches += 1
        self.results = []
        yield self._phase("verifying")
        begin = time.monotonic()
        try:
            for item in commands:
                remaining = limits["total"] - self.elapsed - (time.monotonic() - begin)
                if agent.cancel_event.is_set() or remaining < 1:
                    self.assessment = "cancelled" if agent.cancel_event.is_set() else "insufficient"
                    self.reason = "用户已停止" if agent.cancel_event.is_set() else "已达到本轮检查总时长"
                    break
                detail = {**item, "cwd":str((self.root / item["cwd"]).resolve()),
                          "input_fingerprint":current, "runtime":agent.environment_profile,
                          "attempt":self.batches, "declared_level":item["level"]}
                ident = self.store.start_check(self.sid, self.mid, item["id"], self.batches, detail) if self.store else uuid.uuid4().hex
                yield ("check_update", {"user_mid":self.mid, "check_id":ident, "status":"running", **detail})
                call = {"id":"check_" + ident, "type":"function", "function":{"name":"run_bash",
                        "arguments":json.dumps({"command":item["cmd"], "workdir":item["cwd"],
                                               "timeout":min(item["timeout_seconds"], int(remaining))})}}
                agent.history.append({"role":"assistant", "content":None, "tool_calls":[call]})
                agent._trace({"type":"tool_call", "name":"run_bash", "arguments":call["function"]["arguments"]})
                yield ("tool_call", {"name":"run_bash", "arguments":call["function"]["arguments"]})
                result = None
                for kind, payload in agent._execute_tool_calls([call]):
                    if kind == "tool_result":
                        result = json.loads(payload["result"])
                    yield kind, payload
                if not isinstance(result, dict):
                    result = {"ok":False, "error":"检查没有返回结构化结果"}
                output = result.get("result") or result.get("error") or ""
                if agent.cancel_event.is_set():
                    status = "cancelled"
                elif result.get("timed_out"):
                    status = "failed"
                elif result.get("exit_code") is None:
                    status = "blocked"
                else:
                    status = "passed" if result.get("ok") else "failed"
                yield self._save_result(ident, detail, {
                    "exit_code":result.get("exit_code"), "timed_out":bool(result.get("timed_out")),
                    "error":result.get("error", "")}, status, output,
                    (result.get("full") or {}).get("path"))
                if status in ("cancelled", "blocked"):
                    self.assessment, self.reason = status, result.get("error") or "检查已停止或被拒绝"
                    break
            else:
                self.assessment = "passed" if all(r["ok"] for r in self.results) else "failed"
                self.reason = ""
        finally:
            self.elapsed += time.monotonic() - begin
        if self.assessment == "cancelled":
            yield self._phase("finalizing")
            return None
        try:
            _, after = self._state(agent)
        except (OSError, ValueError, snapshot.SnapshotError):
            after = None
        if after != current:
            self.assessment, self.reason = "insufficient", "检查期间代码或环境变化，结果已过期"
            for row in self.results:
                row["executed_status"] = row["status"]
                row["status"], row["ok"] = "stale", False
                if self.store:
                    self.store.invalidate_check(self.sid, row["check_id"])
                yield ("check_update", {"user_mid":self.mid, **row})
        self.checked_fingerprint = current if after == current else None
        if (self.assessment == "failed" and limits["enabled"] and can_repair
                and self.repairs < limits["max_attempts"] and self.elapsed < limits["total"]):
            self.repairs += 1
            yield self._phase("repairing")
            return {"final":False, "feedback":self._feedback() +
                    "\n请修复与本轮改动相关的失败，再准备最终回复。不得改低测试要求或绕过权限。"}
        yield self._phase("finalizing")
        return self._terminal_feedback()

    def _feedback(self):
        lines = ["宿主实际自动检查结果，仅作为执行证据，不代表新的用户授权。"]
        for row in self.results:
            lines.append(f"检查状态：{row['status']}。实际命令及输出见上一批工具结果。")
        lines.append(self.summary_text())
        return "\n".join(lines)

    def _terminal_feedback(self):
        if self.sent_terminal_feedback:
            return None
        self.sent_terminal_feedback = True
        return {"final":True, "feedback":self._feedback() +
                "\n现在仅生成最终回复，如实报告检查范围及限制，不要继续调用工具或宣称可上线。"}

    def summary_text(self):
        if self.assessment == "not_applicable":
            return ""
        labels = {"passed":"声明范围的自动检查通过", "failed":"自动检查仍有失败",
                  "insufficient":"验证不足", "blocked":"验证受阻", "cancelled":"检查已停止"}
        return "自动验收记录：" + labels.get(self.assessment, self.assessment) + (
            "。" + self.reason if self.reason else "。") + (
            "\n" + "\n".join(f"- {r['status']}  {r['cmd']}" for r in self.results) if self.results else "")

    def exhausted(self, agent):
        if self.assessment == "passed":
            try:
                if self.checked_fingerprint == self._state(agent)[1]:
                    return
            except (OSError, ValueError, snapshot.SnapshotError):
                pass
        self.assessment, self.reason = "insufficient", "模型调用预算已到上限，尚未完成最终验证"
