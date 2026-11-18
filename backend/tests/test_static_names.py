"""静态检查：模块级未定义名字（NameError 的静态版）。

为什么自带一个而不是引 ruff
--------------------------
项目至今零第三方依赖之外的约束，加一个 linter 依赖意味着多一份安装失败面。
而 `except Exception: pass` 时代留下的最实在的风险只有一个：**某个分支里
引用了一个拼错的/没导入的名字**，只在特定路径才炸。这个检查用 AST 覆盖它，
不需要任何外部工具。

检查范围（刻意保守，宁漏勿误报）：
  · 模块级顶层语句里引用的名字，必须能从 import / 赋值 / 内建里找到
  · 函数体内**不**检查（局部作用域、闭包、延迟导入会让准确率很低）

跑法：cd backend && python3 -m unittest tests.test_static_names -v
"""

import ast
import builtins
import pathlib
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]

# unittest 与鸭子类型带来的隐式名字；宁可列白名单也不放宽到误报
_ALLOWED = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "self", "cls",
}


def _module_names(tree: ast.Module) -> set[str]:
    """收集模块级可见的名字：import、赋值目标、函数/类定义、try/except 名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def undefined_at_module_level(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return [f"{path.name}:{e.lineno} 语法错误 {e.msg}"]
    known = _module_names(tree) | _ALLOWED
    problems: list[str] = []
    for node in tree.body:                       # 只看顶层语句
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in known:
                    problems.append(f"{path.name}:{sub.lineno} 未定义名字 {sub.id}")
    return problems


class StaticNameTest(unittest.TestCase):
    def test_no_undefined_module_level_names(self):
        problems: list[str] = []
        for p in sorted(BACKEND.glob("*.py")):
            problems.extend(undefined_at_module_level(p))
        for sub in ("services", "routes", "contracts"):
            for p in sorted((BACKEND / sub).glob("*.py")):
                problems.extend(undefined_at_module_level(p))
        self.assertEqual(problems, [], "模块级存在未定义名字：\n" + "\n".join(problems[:20]))

    def test_all_backend_files_compile(self):
        """语法面全绿：任何 .py 都不该有编译错误（含 tests）。"""
        bad = []
        for p in BACKEND.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as e:
                bad.append(f"{p.relative_to(BACKEND)}:{e.lineno} {e.msg}")
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
