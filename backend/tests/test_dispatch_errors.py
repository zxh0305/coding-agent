"""路由分发层的错误处理契约（回归：异常处理器自己崩了）
================================================================================

cd backend && python3 -m unittest tests.test_dispatch_errors -v

背景（真实踩过的坑）
--------------------------------------------------------------------------------
app.py 拆分到 routes/ 时，do_POST 的异常处理链被逐字搬移，但顶层的
`import json` 没跟着搬。于是 dispatch.py:298 的

    except json.JSONDecodeError as e:

在**求值 except 子句**时就抛 `NameError: name 'json' is not defined`——
Python 匹配 except 会先求值它，所以只要 try 块里抛出任何异常，异常处理器
自己就成了抛异常的那个。

后果比"某个接口报错"严重得多：`json.JSONDecodeError` / `ValueError` /
`SystemExit` / 兜底 `except Exception`（本该记 500 + 堆栈）**全部失效**，
异常逃到 socketserver，socket 直接关闭、无任何 HTTP 响应，客户端看到
`RemoteDisconnected`。而 socketserver 的 handle_error 走 stderr、不进
agent.log——**表象是"后端挂了"，真因是异常处理器崩了**，排查极难。

这组用例锁两件事：
1. 行为：坏请求体必须拿到**合法的 JSON 错误响应**，而不是空响应/连接断开；
2. 缺陷类：dispatch.py 里每个 `except <名>.<属性>` 引用的名必须真的导入过。
   第 2 条是通用护栏——搬移代码时"搬了 except 没搬 import"这类回归，
   靠第 1 条只能覆盖到 json，靠 AST 检查能覆盖以后所有同类漏搬。
"""

import ast
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.test_session_model import ApiTestBase

DISPATCH_PY = Path(__file__).resolve().parent.parent / "routes" / "dispatch.py"
ROUTES_DIR = Path(__file__).resolve().parent.parent / "routes"


def _module_level_names(tree) -> set[str]:
    """模块顶层可见的名字：import 进来的 + 顶层 def/class/赋值 + 内置。

    只收【模块级】定义——函数内部的局部变量在模块作用域不可见，若把
    `log = ...` 写在某个函数里就当成已定义，护栏会漏报。
    """
    import builtins
    names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def undefined_except_roots(path: Path) -> list[tuple[str, int]]:
    """找出 `except 名.属性` 里未定义的名，返回 [(名, 行号)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    known = _module_level_names(tree)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            if isinstance(node.type, ast.Attribute) and isinstance(node.type.value, ast.Name):
                root = node.type.value.id
                if root not in known:
                    bad.append((root, node.lineno))
    return bad


def undefined_attr_roots(path: Path, attr: str) -> list[tuple[str, int]]:
    """找出所有 `名.属性` 中 attr 匹配、但"名"未定义的位置。

    比 except 守卫更宽：凡是引用 `something.warning(...)` 这类跨模块调用，
    若 something 忘了 import，运行时就是 NameError。这类错在【冷路径】上
    （本例是"连接数超限"才走到）平时测不出来，上线第一次触发时把 503 变成
    500，反而更难查。静态扫一遍全量覆盖最划算。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    known = _module_level_names(tree)
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == attr
                and isinstance(node.value, ast.Name) and node.value.id not in known):
            bad.append((node.value.id, node.lineno))
    return bad



class TestBadBodyGetsJsonError(ApiTestBase):
    """坏请求体 → 必须是合法 JSON 错误响应，而不是静默断开。"""

    def raw_post(self, path, body: bytes, headers=None):
        """发原始字节，绕过 ApiTestBase.call 的 json.dumps（我们要发**坏** JSON）。

        返回 (status, body)。连接被服务端掐断时 urllib 抛 RemoteDisconnected
        ——正是本 bug 的表象，这里把它转成断言失败而不是错误，报错信息更直白。
        """
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # RemoteDisconnected / ConnectionReset 等
            self.fail(f"服务端未返回 HTTP 响应（连接被掐断）：{type(e).__name__}: {e}")

    def test_malformed_json_body_returns_400(self):
        """核心回归：非法 JSON body 过去得到空响应，现在必须是 400 + JSON。"""
        status, raw = self.raw_post("/api/auth/login", b"{not json",
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        body = json.loads(raw)          # 必须是合法 JSON，不是 HTML 错误页
        self.assertIn("JSON", body["error"])
        self.assertTrue(body["error"].strip())   # 文案非空，前端能显示

    def test_malformed_json_on_authed_post_returns_400(self):
        """带鉴权的 POST 同样走这条链（覆盖 try 块里靠后的分支）。"""
        status, raw = self.raw_post("/api/sessions", b"{{{",
                                    {"Content-Type": "application/json",
                                     "Authorization": "Bearer " + self.token})
        self.assertEqual(status, 400)
        json.loads(raw)  # 必须是合法 JSON，不是 HTML 错误页

    def test_non_numeric_content_length_returns_400(self):
        """Content-Length 非数字：int() 抛 ValueError，须走 ValueError 分支。

        这条同时证明 `except ValueError` 也还活着（同一个 NameError 会一并废掉
        它后面的所有分支）。
        """
        status, raw = self.raw_post("/api/auth/login", b"{}",
                                    {"Content-Type": "application/json",
                                     "Content-Length": "abc"})
        self.assertEqual(status, 400)
        json.loads(raw)

    def test_error_response_is_always_json(self):
        """错误路径必须是项目统一的 JSON 契约（前端按 .error 取文案）。"""
        status, raw = self.raw_post("/api/auth/login", b"[[[",
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIsInstance(json.loads(raw).get("error"), str)


class TestExceptClausesOnlyUseImportedNames(unittest.TestCase):
    """通用护栏：`except 名.属性` 里的名必须真的导入过。

    这类缺陷（搬代码时漏搬 import）用行为用例只能一条条碰运气覆盖；
    用 AST 静态检查一次锁死整个文件，且对未来新增的同类漏搬同样有效。
    """

    def test_every_except_name_is_imported(self):
        bad = undefined_except_roots(DISPATCH_PY)
        self.assertEqual(
            bad, [],
            f"dispatch.py 的 except 子句引用了未导入的名字 {bad}——"
            f"匹配异常时会 NameError，让该处及**其后所有** except 分支全部失效，"
            f"异常逃逸导致连接被关闭、无 HTTP 响应（历史事故：漏搬 import json）")

    def test_guard_detects_the_real_bug(self):
        """护栏自检：喂一段"搬了 except 没搬 import"的代码，必须判为违规。

        没有这条，护栏可能因为写错（比如 known 集合过宽）而永远绿灯——
        永久绿的护栏比没有护栏更糟，它给人虚假的安全感。
        """
        import tempfile
        code = "import re\ndef f():\n    try:\n        pass\n    except json.JSONDecodeError:\n        pass\n"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(code)
            path = Path(fh.name)
        try:
            offenders = undefined_except_roots(path)
        finally:
            path.unlink()
        self.assertEqual([n for n, _ in offenders], ["json"])


class TestRouteModulesHaveNoUndefinedNames(unittest.TestCase):
    """全 routes/ 扫一遍：`except 名.属性` 与跨模块调用里的"名"必须已定义。

    为什么值得全扫：这类缺陷【只在冷路径触发】，平时测试跑不到。
    本项目的实例不止一个——
      * dispatch.py 的 `except json.JSONDecodeError`（漏 import json，任何
        POST 异常都静默断连）；
      * sessions.py 的 `log.warning(...)`（该文件用的是 busref.log，裸 log
        未定义，只在"SSE 连接数超限"这条冷路径上炸出 NameError，
        503 变成 500）。
    两次都是同一个成因（引用了没导入的名字），静态检查一次全覆盖。
    """

    def test_no_undefined_except_roots(self):
        bad = {}
        for p in sorted(ROUTES_DIR.glob("*.py")):
            offenders = undefined_except_roots(p)
            if offenders:
                bad[p.name] = offenders
        self.assertEqual(
            bad, {},
            f"routes/ 下有 except 子句引用未定义的名 {bad}——匹配异常时 NameError，"
            f"令该处及其后所有 except 失效，异常逃逸导致连接被关闭、无 HTTP 响应")

    def test_no_undefined_log_or_json_roots(self):
        """最常被误用的两个跨模块名：log 与 json。

        这两个在本项目里反复出错（每个模块的 log 来源还不一样：app 用
        logging.getLogger("app")、sessions 用 busref.log），值得单独锁。
        """
        bad = {}
        for p in sorted(ROUTES_DIR.glob("*.py")):
            hits = undefined_attr_roots(p, "warning") + undefined_attr_roots(p, "dumps")
            if hits:
                bad[p.name] = hits
        self.assertEqual(
            bad, {},
            f"routes/ 下有未定义的跨模块调用 {bad}——首次触达该冷路径时会抛 "
            f"NameError，把预期的 4xx/503 变成 500，且只在日志里才看得到")

    def test_guard_detects_the_sessions_bug(self):
        """护栏自检：喂一段"用了未导入的 log"的代码，必须判为违规。

        没有这条，护栏可能因 known 集合过宽（比如把函数内局部变量也算进来）
        而永远绿灯——永久绿的护栏比没有护栏更糟。
        """
        import tempfile
        code = ("import os\n\n"
                "def g():\n"
                "    log.warning('y')\n"     # 模块级 log 未定义 → 违规
                "    json.dumps({})\n")       # 模块级 json 未定义 → 违规
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(code)
            path = Path(fh.name)
        try:
            hits = undefined_attr_roots(path, "warning") + undefined_attr_roots(path, "dumps")
        finally:
            path.unlink()
        self.assertEqual(sorted(n for n, _ in hits), ["json", "log"],
                         "未导入的跨模块调用必须全部报出")

    def test_local_variable_does_not_satisfy_module_scope(self):
        """局部变量不得让模块级引用变合法——否则护栏形同虚设。

        写法上很容易踩：想到"函数里给 log 赋了值就算定义了吧"，
        护栏就会对真正的缺陷保持沉默。
        """
        import tempfile
        code = ("def f():\n"
                "    log = None\n"          # 局部：只在 f 内可见
                "    return log\n\n"
                "def g():\n"
                "    log.warning('y')\n")  # 模块级仍缺 log → 必须报出
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(code)
            path = Path(fh.name)
        try:
            hits = undefined_attr_roots(path, "warning")
        finally:
            path.unlink()
        self.assertEqual([n for n, _ in hits], ["log"],
                         "局部 log 不算模块级定义，模块级缺 log 必须报出")


if __name__ == "__main__":
    unittest.main()
