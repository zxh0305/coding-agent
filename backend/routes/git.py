"""Git 路由：只读浮窗数据源 + 分支切换（唯一的 git 写操作）
=======================================================

从原 app.py 的 Handler 逐字搬移：_handle_git / _handle_git_checkout。
"""

import re
from pathlib import Path

import db
from git_tools import GitError, repo_summary
from git_tools import branches as git_branches
from git_tools import checkout as git_checkout
from git_tools import identity as git_identity
from git_tools import log as git_log
from git_tools import show as git_show
from services.workspace import _resolve_workspace

# 提交 hash 白名单：只允许 7-40 位十六进制（短 hash / 完整 SHA-1）。
# 这是 /api/git/show 的第一道防线——hash 会作为 argv 传给 git，虽然
# shell=False 已经免疫命令注入，但限制字符集能挡掉"传个分支名/选项
# 进来"（如 --output=… 这类被误当参数的形态）。
_GIT_HASH_RE = re.compile(r"[0-9a-fA-F]{7,40}")


class GitRoutes:
    def _handle_git(self, path: str):
        """Git 浮窗数据源：/api/git/log、/api/git/show、/api/git/summary。

        全部只读（git log/show/config），因此不过权限闸门。工作区按 session_id
        解析（与 Agent 工具用的是同一套 _resolve_workspace）——浮窗看到的就是
        当前任务真正在操作的目录，不是服务进程的 cwd。
        """
        sid = (self._query().get("session_id") or [""])[0]
        if sid and db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        # workspace 直传（新任务态）：会话还没创建时前端预选了项目，徽章/浮窗
        # 也要能立刻显示该项目的分支名——传目录路径直接查询。目录必须真实存在，
        # 且只允许绝对路径（与提交消息绑工作区同一套校验思路）；不传则走会话解析。
        ws_req = (self._query().get("workspace") or [""])[0]
        if ws_req and not sid:
            ws_target = Path(ws_req).expanduser().resolve()
            if not ws_target.is_dir() or ws_target == Path(ws_target.root):
                return self._json({"ok": False, "reason": "no_workspace",
                                   "error": "目录不存在或不可用"}, 200)
            ws = ws_target
        else:
            ws = _resolve_workspace(self.user["id"], sid)
        if ws is None:
            return self._json({"ok": False, "reason": "no_workspace",
                               "error": "该任务还没有绑定项目文件夹"}, 200)
        try:
            if path == "/api/git/summary":
                return self._json({"ok": True, **repo_summary(ws),
                                   "identity": git_identity(ws)})
            if path == "/api/git/log":
                qs = self._query()
                try:
                    limit = min(100, max(1, int((qs.get("limit") or ["30"])[0])))
                    offset = max(0, int((qs.get("offset") or ["0"])[0]))
                except ValueError:
                    return self._json({"error": "limit/offset 须为整数"}, 400)
                # author=me 时用本机 git email 过滤；author=other 时前端拿到
                # 全量后自行剔除自己（git --author 不支持"非"语义）
                who = (qs.get("author") or ["all"])[0]
                me = git_identity(ws).get("email", "")
                author = me if who == "me" and me else ""
                data = git_log(ws, limit=limit, offset=offset, author=author)
                if who == "other" and me:
                    data["commits"] = [c for c in data["commits"]
                                       if c["email"].lower() != me.lower()]
                return self._json({"ok": True, **repo_summary(ws), **data})
            if path == "/api/git/show":
                commit_hash = (self._query().get("hash") or [""])[0]
                if not _GIT_HASH_RE.fullmatch(commit_hash):
                    return self._json({"error": "非法的提交 hash"}, 400)
                return self._json({"ok": True, **git_show(ws, commit_hash)})
            if path == "/api/git/branches":
                return self._json({"ok": True, **git_branches(ws)})
            return self._json({"error": "未知的 git 接口"}, 404)
        except GitError as e:
            # 不是仓库 / 空仓库（无提交）等都从这里出去：前端显示提示文案，
            # 不是错误弹窗——"这个文件夹不是 git 仓库"是正常状态而非故障
            return self._json({"ok": False, "reason": "not_repo",
                               "error": str(e)}, 200)

    def _handle_git_checkout(self):
        """切换工作区所在仓库的分支（本模块唯一的 git 写操作）。

        分支名必须已在本地分支白名单里（git_tools.checkout 里校验），因此
        不接受任意字符串；工作树有未提交改动时 git 会拒绝并原样报错——
        绝不 --force 丢弃用户的改动。
        """
        body = self._body()
        sid = str(body.get("session_id") or "")
        if sid and db.session_owner(sid) != self.user["id"]:
            return self._json({"error": "任务不存在或不属于当前用户"}, 404)
        ws = _resolve_workspace(self.user["id"], sid)
        if ws is None:
            return self._json({"ok": False, "reason": "no_workspace",
                               "error": "该任务还没有绑定项目文件夹"}, 200)
        branch = str(body.get("branch") or "").strip()
        if not branch:
            return self._json({"error": "branch 不能为空"}, 400)
        try:
            git_checkout(ws, branch)
            return self._json({"ok": True, **repo_summary(ws),
                               **git_branches(ws)})
        except GitError as e:
            # 未提交改动冲突 / 分支不存在等：把 git 的原话给用户看
            return self._json({"ok": False, "error": str(e)}, 200)
