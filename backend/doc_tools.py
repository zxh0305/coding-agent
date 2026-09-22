"""
文档工具（Document tools）
==========================

让 agent 能把工作成果（汇报、总结、方案）写成 Markdown 文档，供用户在
右侧面板查看。与 code_tools 分开是因为职责不同：code_tools 操作的是
【工作区文件】（用户的项目代码），本模块操作的是【会话文档】（agent 的
产出物，按会话隔离存在 data/docs/<sid>/，不进用户工作区）。

与工具层的约定一致：
  * 工具层不持有全局状态——session_id 通过 ToolContext 注入（见 tools.py）；
  * 失败走统一信封 {ok:false, error, hint?}，让模型能读原因改道；
  * ctx 形参不进 schema（模型看不到），由 execute_tool 在执行时注入。
"""

import json

import db


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str, hint: str = "") -> str:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


def create_doc(name: str, content: str, ctx=None) -> str:
    """生成/更新一份 Markdown 文档，写入当前会话的文档目录。

    name 是文件名（自动补 .md，不含路径）；content 是完整 Markdown 正文。
    同名即覆盖（=更新）。文档按会话隔离存放在 data/docs/<session_id>/，
    用户在右侧面板查看。
    """
    sid = getattr(ctx, "session_id", None) if ctx is not None else None
    if not sid:
        return _err("当前会话未知，无法确定文档归属（系统内部问题）",
                    "请重试；若持续失败请联系服务部署者")
    try:
        info = db.write_doc(sid, name, content)
    except ValueError as e:
        return _err(f"文档写入被拒：{e}",
                    "换一个不含路径分隔符的文件名；content 为完整 Markdown 正文")
    except OSError as e:
        return _err(f"文档写入失败：{e}", "稍后重试")
    return _ok({"name": info["name"], "bytes": info["bytes"], "lines": info["lines"],
                "result": f"已生成文档《{info['name']}》（{info['bytes']} 字节 / "
                          f"{info['lines']} 行），用户可在右侧文档面板查看。"})


DOC_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_doc",
            "description": "生成一份 Markdown 文档并展示给用户（右侧文档面板）。"
                           "什么时候用：当用户要求「生成文档 / 写一份汇报 / 总结 / 方案 / 报告」时用它，"
                           "把工作成果整理成结构化的 Markdown（含标题、列表、表格等）。"
                           "content 必须是【完整正文】而不是摘要或对用户的说明；"
                           "文档按会话隔离保存，同名会覆盖（即更新）。"
                           "示例：{\"name\": \"重构汇报\", \"content\": \"# 重构汇报\\n\\n## 背景\\n...\"}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "文档文件名，如 '重构汇报'（自动补 .md，不含路径）"},
                    "content": {"type": "string",
                                "description": "完整的 Markdown 正文（含标题、列表、表格等），不是摘要"},
                },
                "required": ["name", "content"],
            },
        },
    },
]

DOC_TOOL_REGISTRY = {
    "create_doc": create_doc,
}

# 文档生成视为低危操作：不弹确认卡（详见 permissions.py 的豁免）。
# 但它确实会写磁盘，标记为非只读（False），与其它写工具一样串行调度。
DOC_TOOL_READ_ONLY = {
    "create_doc": False,
}
