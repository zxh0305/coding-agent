"""架构门禁：用测试守住分层方向，防止依赖重新烂掉。

为什么需要
----------
`agent.py` 2326 行、`db.py` 2839 行、`app.js` 8230 行——规模本身不是问题，
真正的问题是**依赖方向失控**之后没法再拆。ZCode 用 architecture-policy.yaml
把这件事交给机器，这里用一份 50 行的 import 检查达到同样的目的。

只检查四条最容易崩、也最容易自动判定的规则（不做过度设计）：

  R1  routes/*       不得 import app（入口不能反过来依赖路由）
  R2  services/*     不得 import app，也不得 import routes
  R3  叶子模块       不得 import agent / app（否则谁都能拉起整个运行时）
  R4  db / logger    不得 import 任何业务模块（基础层必须是最底层）

为什么这些规则值得守：反向依赖会制造 import 期循环——本项目已经踩过一次
（busref 注入点被 import 期取成 None 导致会话列表 500）。那次修完靠的是人读代码；
这条测试让同类问题在 CI 里直接红。
"""

import ast
import pathlib
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def imports_of(path: pathlib.Path, *, module_level_only: bool = True) -> set[str]:
    """取一个 .py 文件的 import 顶层模块名。

    module_level_only=True（默认）只统计**模块级**导入。函数内部的延迟导入
    （`def f(): import memory`）是有意的破环手法——它不产生 import 期依赖，
    不影响分层，算进来只会得到误报。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()

    def collect(nodes) -> set[str]:
        names: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # 相对导入（from . import x）不算跨层
                    continue
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    if module_level_only:
        return collect(tree.body)
    return collect(ast.walk(tree))


def files_in(sub: str) -> list[pathlib.Path]:
    return [p for p in (BACKEND / sub).glob("*.py") if p.name != "__init__.py"]


class LayeringTest(unittest.TestCase):
    def test_routes_do_not_import_app(self):
        offenders = [p.name for p in files_in("routes") if "app" in imports_of(p)]
        self.assertEqual(offenders, [], f"routes 反向依赖了 app：{offenders}")

    def test_services_do_not_import_app_or_routes(self):
        bad = []
        for p in files_in("services"):
            names = imports_of(p)
            if "app" in names or "routes" in names:
                bad.append(p.name)
        self.assertEqual(bad, [], f"services 反向依赖了 app/routes：{bad}")

    def test_leaf_modules_do_not_import_agent(self):
        """基础模块（db/logger/events/契约…）不能反过来拉起运行时。

        agent 依赖它们；它们再 import agent 就成环，import 期拿到半成品模块
        （busref 那次 500 的同类问题）。
        """
        leaves = ["db.py", "logger.py", "events.py", "turn_state.py",
                  "crash_tail.py", "diagnostics.py", "stability_metrics.py",
                  "contracts"]
        bad = []
        for name in leaves:
            p = BACKEND / name
            if p.is_dir():
                candidates = list(p.glob("*.py"))
            else:
                candidates = [p] if p.exists() else []
            for c in candidates:
                if "agent" in imports_of(c) or "app" in imports_of(c):
                    bad.append(f"{c.relative_to(BACKEND)}")
        self.assertEqual(bad, [], f"基础模块反向依赖了运行时：{bad}")

    def test_db_and_logger_import_no_business_modules(self):
        """基础层最底层：db/logger 不应 import 任何业务模块。"""
        business = {"agent", "tools", "services", "routes", "memory",
                    "permissions", "llm_client", "skills", "browser_tools"}
        bad = []
        for name in ("db.py", "logger.py"):
            names = imports_of(BACKEND / name)
            hit = names & business
            if hit:
                bad.append(f"{name} → {sorted(hit)}")
        self.assertEqual(bad, [], f"基础层依赖了业务模块：{bad}")


class LazyImportTest(unittest.TestCase):
    def test_db_memory_is_lazy_not_module_level(self):
        """db 里对 memory 的引用只允许是函数内延迟导入。

        memory 需要 db，db 若在模块级反过来 import memory 就成环；
        现在的写法是有意的破环手法，这条测试把它钉住——
        哪天有人手滑把 `import memory` 挪到文件顶部，这里立刻红。
        """
        module_level = imports_of(BACKEND / "db.py")
        self.assertNotIn("memory", module_level)
        # 但延迟导入确实存在（不是为了绕过门禁而删掉了功能）
        source = (BACKEND / "db.py").read_text(encoding="utf-8")
        self.assertIn("import memory", source)


class CycleTest(unittest.TestCase):
    def test_no_import_cycle_among_leaf_modules(self):
        """基础模块之间不能成环（成环 = import 期顺序决定行为，极难排查）。"""
        leaves = ["db", "logger", "events", "turn_state", "crash_tail",
                  "diagnostics", "stability_metrics"]
        graph = {n: imports_of(BACKEND / f"{n}.py") & set(leaves) for n in leaves}

        # 三色 DFS 找环
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in leaves}
        stack: list[str] = []
        cycles: list[str] = []

        def visit(n: str):
            color[n] = GRAY
            stack.append(n)
            for m in graph.get(n, ()):
                if color.get(m, BLACK) == GRAY:
                    cycles.append(" → ".join(stack[stack.index(m):] + [m]))
                elif color.get(m, BLACK) == WHITE:
                    visit(m)
            stack.pop()
            color[n] = BLACK

        for n in leaves:
            if color[n] == WHITE:
                visit(n)
        self.assertEqual(cycles, [], f"发现 import 环：{cycles}")


if __name__ == "__main__":
    unittest.main()
