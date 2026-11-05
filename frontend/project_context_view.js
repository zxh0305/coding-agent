/* Project metadata is text-only. Loading an environment never executes its Python. */
(() => {
  const dialog = $("project-context-dialog"), body = $("project-context-body");
  const status = $("project-context-status");
  let request = 0;
  const close = () => { request++; dialog.close(); };
  function target() {
    const query = new URLSearchParams();
    if (currentSession) query.set("session_id", currentSession);
    else if (pendingProjectPath) query.set("workspace", pendingProjectPath);
    else return null;
    if (currentSession && wsCustom) query.set("workspace", $("ws-pick").title);
    return "/api/project-context?" + query;
  }
  async function load(executable) {
    const url = target();
    if (!url || workspacePending) { toast("请先确认当前项目"); return; }
    const seq = ++request, view = sessionViewVersion, generation = workspaceGeneration;
    const isCurrent = () => seq === request && view === sessionViewVersion && generation === workspaceGeneration;
    status.textContent = executable === undefined ? "正在读取项目设置…" : "正在保存环境选择…";
    const openedRules = new Set([...body.querySelectorAll("details[open]")].map(el => el.dataset.path));
    if (executable === undefined) body.replaceChildren();
    body.inert = executable !== undefined;
    status.dataset.state = "loading";
    if (!dialog.open) dialog.showModal();
    try {
      const data = await api(url, executable === undefined ? {} : {
        method:"POST", body:JSON.stringify({executable})
      });
      if (!isCurrent()) return;
      status.textContent = executable === undefined ? "" : "已保存，下一条消息使用该环境";
      status.dataset.state = "success";
      const content = document.createDocumentFragment();
      const project = document.createElement("div"); project.className = "pc-project";
      const name = document.createElement("strong");
      const workspace = data.environment.workspace || "";
      name.textContent = workspace.split(/[\\/]/).filter(Boolean).at(-1) || "当前项目";
      const path = document.createElement("span"); path.textContent = workspace; path.title = workspace;
      project.append(name, path); content.append(project);
      const environment = document.createElement("section"); environment.className = "pc-card";
      const heading = document.createElement("h3"); heading.textContent = "运行环境";
      const caption = document.createElement("p"); caption.className = "pc-caption";
      caption.textContent = "选择这个项目执行命令时使用的 Python 环境。";
      const controls = document.createElement("div"); controls.className = "pc-environment-controls";
      const select = document.createElement("select"); select.setAttribute("aria-label", "项目 Python 解释器");
      const system = document.createElement("option"); system.value = "system"; system.textContent = "系统环境";
      select.append(system);
      for (const c of data.environment.candidates) {
        const option = document.createElement("option"); option.value = c.path; option.textContent = c.label;
        select.append(option);
      }
      const unresolved = data.environment.blocked || (!data.environment.selected && !data.environment.source && data.environment.candidates.length > 1);
      if (unresolved) {
        const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.disabled = true;
        placeholder.textContent = data.environment.blocked ? "原环境已失效，请重新选择" : "发现多个环境，请选择";
        select.prepend(placeholder);
      }
      const saved = unresolved ? "" : data.environment.selected?.path || "system";
      select.value = saved;
      const save = document.createElement("button"); save.type = "button"; save.className = "pc-save";
      save.textContent = "保存环境选择"; save.disabled = true;
      select.addEventListener("change", () => { save.disabled = !select.value || select.value === saved; });
      save.onclick = () => { save.disabled = true; save.textContent = "保存中…"; load(select.value); };
      controls.append(select, save);
      const note = document.createElement("p"); note.className = "pc-note";
      note.textContent = "环境是否可用、依赖是否完整，以实际运行检查为准。";
      environment.append(heading, caption, controls, note); content.append(environment);
      const warnings = [...data.environment.diagnostics, ...data.rules.diagnostics]
        .filter(warning => !(unresolved && !data.environment.blocked && warning.startsWith("发现多个 Python 环境")));
      if (warnings.length) {
        const list = document.createElement("ul"); list.className = "pc-warnings";
        for (const warning of warnings) {
          const item = document.createElement("li"); item.textContent = warning.replaceAll("项目上下文", "项目设置"); list.append(item);
        }
        content.append(list);
      }
      const rules = document.createElement("section"); rules.className = "pc-card pc-rules";
      const title = document.createElement("h3"); title.textContent = "项目规则";
      const count = document.createElement("span"); count.className = "pc-count";
      count.textContent = `${data.rules.sources.length} 份`; title.append(count);
      const hint = document.createElement("p"); hint.className = "pc-caption";
      hint.textContent = "Agent 会自动加载，点击文件名查看内容。";
      rules.append(title, hint);
      if (!data.rules.sources.length) {
        const p = document.createElement("p"); p.className = "pc-empty";
        p.textContent = "还没有项目规则，可在项目目录添加 AGENTS.md 或 CLAUDE.md。"; rules.append(p);
      }
      for (const source of data.rules.sources) {
        const details = document.createElement("details"), summary = document.createElement("summary");
        details.dataset.path = source.path;
        details.open = executable !== undefined && openedRules.has(source.path);
        summary.textContent = source.path;
        const pre = document.createElement("pre"); pre.textContent = source.content;
        details.append(summary, pre); rules.append(details);
      }
      content.append(rules); body.replaceChildren(content);
    } catch (e) {
      if (isCurrent()) {
        status.textContent = (executable === undefined ? "读取失败：" : "保存失败：") + e.message;
        status.dataset.state = "error";
        const save = body.querySelector(".pc-save");
        if (save) { save.disabled = false; save.textContent = "保存环境选择"; }
        else {
          const retry = document.createElement("button"); retry.className = "pc-retry"; retry.textContent = "重新加载";
          retry.onclick = () => load(); body.replaceChildren(retry);
        }
      }
    } finally { if (isCurrent()) body.inert = false; }
  }
  $("project-context-open").onclick = () => load();
  $("project-context-close").onclick = close;
  dialog.addEventListener("cancel", () => request++);
  window.ProjectContextUI = {close};
})();
