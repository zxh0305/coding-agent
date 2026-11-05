"""Additive execution semantics; never change legacy ok/exit_code or raw evidence."""
import json
import shlex
from pathlib import Path


def annotate(name, arguments, result):
    try:
        info = json.loads(result)
    except (ValueError, TypeError):
        return result
    if not isinstance(info, dict) or not isinstance(info.get("ok"), bool):
        return result
    code = info.get("error_code")
    error = str(info.get("error", ""))
    if info.get("cancelled"):
        outcome, code = "cancelled", code or "CANCELLED"
    elif info.get("timed_out"):
        outcome, code = "timeout", code or "TIMEOUT"
    elif code == "PERMISSION_DENIED" or error.startswith("权限拒绝:"):
        outcome, code = "denied", "PERMISSION_DENIED"
    elif code == "EXECUTOR_ERROR":
        outcome = "executor_error"
    elif info["ok"]:
        outcome = "no_match" if name == "grep" and info.get("total") == 0 else "success"
    elif name == "run_bash" and type(info.get("exit_code")) is int:
        outcome, code = "command_exit", code or "COMMAND_EXIT"
        # Only simple commands have unambiguous exit-1 semantics. Pipelines,
        # redirections and compound shell expressions retain their raw status.
        command = (arguments or {}).get("command", "")
        try:
            argv = shlex.split(command)
        except (ValueError, TypeError):
            argv = []
        if info["exit_code"] == 1 and argv and not any(c in command for c in "|&;<>`$\n"):
            executable = Path(argv[0]).name
            if executable in ("grep", "rg"):
                outcome, code = "no_match", "NO_MATCH"
            elif executable == "git" and argv[1:2] == ["diff"] and "--exit-code" in argv:
                outcome, code = "differences", "DIFFERENCES_FOUND"
        if outcome in ("no_match", "differences"):
            info["hint"] = "该退出码表示查询结果，不是工具故障；按原始输出继续判断任务。"
    else:
        outcome, code = "error", code or "TOOL_ERROR"
    info.setdefault("outcome", outcome)
    if code:
        info.setdefault("error_code", code)
    return json.dumps(info, ensure_ascii=False)
