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
    status.textContent = "正在读取项目上下文…";
    body.replaceChildren();
    if (!dialog.open) dialog.showModal();
    try {
      const data = await api(url, executable === undefined ? {} : {
        method:"POST", body:JSON.stringify({executable})
      });
      if (!isCurrent()) return;
      status.textContent = executable === undefined ? data.environment.workspace : "已保存，下一条消息使用该环境";
      const heading = document.createElement("h3"); heading.textContent = "运行环境";
      const select = document.createElement("select"); select.setAttribute("aria-label", "项目 Python 解释器");
      const system = document.createElement("option"); system.value = "system"; system.textContent = "使用系统环境";
      select.append(system);
      for (const c of data.environment.candidates) {
        const option = document.createElement("option"); option.value = c.path; option.textContent = c.label;
        select.append(option);
      }
      select.value = data.environment.selected?.path || "system";
      const save = document.createElement("button"); save.type = "button"; save.textContent = "保存环境选择";
      save.onclick = () => { save.disabled = true; load(select.value); };
      const note = document.createElement("p");
      note.textContent = "这里只识别解释器路径。是否能运行、依赖是否完整，需通过实际检查确认。";
      body.append(heading, select, save, note);
      for (const warning of [...data.environment.diagnostics, ...data.rules.diagnostics]) {
        const p = document.createElement("p"); p.textContent = warning; body.append(p);
      }
      const title = document.createElement("h3"); title.textContent = "项目规则"; body.append(title);
      if (!data.rules.sources.length) {
        const p = document.createElement("p"); p.textContent = "当前项目根目录没有已加载的规则。"; body.append(p);
      }
      for (const source of data.rules.sources) {
        const details = document.createElement("details"), summary = document.createElement("summary");
        summary.textContent = source.path;
        const pre = document.createElement("pre"); pre.textContent = source.content;
        details.append(summary, pre); body.append(details);
      }
    } catch (e) { if (isCurrent()) status.textContent = "读取失败：" + e.message; }
  }
  $("project-context-open").onclick = () => load();
  $("project-context-close").onclick = close;
  dialog.addEventListener("cancel", () => request++);
  window.ProjectContextUI = {close};
})();
