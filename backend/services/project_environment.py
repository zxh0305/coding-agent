"""Discover project runtimes without executing project code or reading secrets."""
import hashlib
import json
import os
from pathlib import Path

MAX_CONFIG_BYTES = 32768


def preference_key(workspace, uid):
    identity = hashlib.sha256(os.fsencode(Path(workspace).resolve())).hexdigest()
    return f"project_runtime:{uid}:{identity}"


def load_config(workspace):
    root = Path(workspace).resolve()
    path = root / ".agents" / "project.json"
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("项目配置不能通过符号链接指向其他目录")
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}, []
    except ValueError as e:
        return {}, [str(e)]
    except OSError:
        return {}, ["无法读取 .agents/project.json"]
    try:
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("项目配置超过32KB")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("项目配置需要 schema_version=1")
        runtime = data.get("runtime", {})
        if not isinstance(runtime, dict) or runtime.get("type", "python") != "python":
            raise ValueError("当前项目配置支持 Python runtime")
        executable = runtime.get("executable")
        if executable is not None and (not isinstance(executable, str) or not executable):
            raise ValueError("解释器路径必须是非空字符串")
        return data, []
    except (ValueError, UnicodeError) as e:
        return {}, [str(e)]


def candidate(root, executable):
    path = root / executable
    if not path.parent.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f"项目解释器不存在或其目录越界：{executable}")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise ValueError(f"项目解释器不可执行：{executable}")
    return {"path": str(path), "label": executable, "status": "discovered"}


def discover(workspace, preferred=None):
    root = Path(workspace).resolve()
    config, diagnostics = load_config(root)
    runtime = config.get("runtime", {})
    configured = runtime.get("executable")
    names = [configured] if configured else []
    for folder in (".venv", "venv"):
        names.append(f"{folder}/Scripts/python.exe" if os.name == "nt" else f"{folder}/bin/python")
    candidates = []
    for name in dict.fromkeys(names):
        try:
            item = candidate(root, name)
            candidates.append(item)
        except ValueError as e:
            if name == configured:
                diagnostics.append(str(e))
    selected, source = None, None
    if preferred == "system":
        source = "user"
    elif preferred:
        selected = next((c for c in candidates if c["path"] == preferred), None)
        source = "user"
        if selected is None:
            diagnostics.append("已选择的项目解释器已失效，请重新选择")
    elif configured:
        selected = next((c for c in candidates if c["label"] == configured), None)
        source = "project"
    elif len(candidates) == 1:
        selected, source = candidates[0], "discovered"
    elif len(candidates) > 1:
        diagnostics.append("发现多个 Python 环境，请在项目上下文中选择")
    markers = [name for name in ("pyproject.toml", "requirements.txt", "package.json",
                                 "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "Makefile")
               if (root / name).is_file()]
    return {"blocked": bool((configured or preferred) and preferred != "system" and not selected),
            "workspace": str(root), "candidates": candidates,
            "selected": selected, "source": source, "markers": markers,
            "diagnostics": diagnostics,
            "status": "unavailable" if diagnostics and not selected else "discovered",
            "verification": "解释器和依赖尚未运行验证"}


def model_context(profile):
    lines = ["\n\n【项目运行环境】", "工作目录：" + profile["workspace"]]
    if profile["selected"]:
        lines.append("项目 Python：" + profile["selected"]["path"])
        lines.append("运行 Python 检查时使用该解释器，先验证所需模块。仅发现路径不代表依赖可用。")
    else:
        lines.append("未选择项目解释器。系统 Python 缺依赖时，先检查下面的项目候选，不能直接断言项目无法测试。")
    for item in profile["candidates"]:
        lines.append("候选解释器：" + item["path"])
    lines += profile["diagnostics"]
    return "\n".join(lines)
