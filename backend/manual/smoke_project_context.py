"""Project context inspector against a temporary DB; no model calls."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

case = ApiTestBase()
case.setUp()
try:
    project = Path(case.tmp).resolve() / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("Use project tests <script>window.leaked=true</script>")
    executable = project / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width":1280, "height":900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("localStorage.setItem('auth_token'," + repr(case.token) +
                             ");localStorage.setItem('auth_username','tester');")
        page.goto(case.base)
        expect(page.locator("#layout")).to_be_visible()
        page.evaluate("(path) => newTask(path)", str(project))
        page.locator("#project-context-open").click()
        expect(page.locator("#project-context-body")).to_contain_text("AGENTS.md")
        page.locator("#project-context-body summary").click()
        expect(page.locator("#project-context-body")).to_contain_text("Use project tests")
        assert not page.evaluate("!!window.leaked")
        page.get_by_label("项目 Python 解释器").select_option(str(executable))
        page.get_by_role("button", name="保存环境选择").click()
        expect(page.locator("#project-context-status")).to_contain_text("下一条消息")
        page.set_viewport_size({"width":390, "height":844})
        assert page.locator("#project-context-dialog").evaluate(
            "(el) => el.getBoundingClientRect().right <= innerWidth")
        page.locator("#project-context-close").click()
        page.locator("#project-context-open").click()
        expect(page.get_by_label("项目 Python 解释器")).to_have_value(str(executable))
        page.evaluate("newTask()")
        expect(page.locator("#project-context-dialog")).not_to_be_visible()
        assert not errors, errors
        browser.close()
    print("PASS: project rules, runtime selection, text-only rendering, navigation and mobile")
finally:
    case.tearDown()
    case.doCleanups()
