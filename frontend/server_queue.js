/* Accepted work belongs to the server; this is a disposable session view. */
window.ServerQueueUI = (() => {
  let generation = 0, editGeneration = 0;
  const bar = document.createElement("div"); bar.id = "server-queue-bar"; bar.className = "hidden";
  document.getElementById("queue-bar").before(bar);
  const dialog = document.createElement("dialog"); dialog.className = "queue-edit-dialog";
  const heading = document.createElement("h3"); heading.textContent = "编辑排队需求";
  const note = document.createElement("p"); note.textContent = "附件、技能和所属项目保持不变。已开始的任务不能修改。";
  const input = document.createElement("textarea"); input.setAttribute("aria-label", "排队需求正文"); input.rows = 5;
  const status = document.createElement("p"); status.setAttribute("role", "status");
  const actions = document.createElement("div"); actions.className = "queue-edit-actions";
  const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "取消"; cancel.onclick = () => dialog.close();
  const save = document.createElement("button"); save.type = "button"; save.textContent = "保存修改";
  dialog.addEventListener("close", () => { editGeneration++; save.disabled = cancel.disabled = false; });
  dialog.addEventListener("cancel", event => { if (save.disabled) event.preventDefault(); });
  actions.append(cancel, save); dialog.append(heading,note,input,status,actions); document.body.append(dialog);
  async function change(sid, item, action, text) {
    if (sid !== currentSession) throw new Error("会话已切换，请重新打开队列");
    return api(`/api/sessions/${encodeURIComponent(sid)}/queue`, {method:"POST",
      body:JSON.stringify({mid:item.mid,revision:item.revision,action,text})});
  }
  async function ensureAvailable(sid) {
    const data = await api(`/api/sessions/${encodeURIComponent(sid)}/queue`);
    if (!Array.isArray(data.items)) throw new Error("队列接口版本不兼容，请更新并重启后端");
  }
  function draw(items, sid) {
    bar.replaceChildren(); bar.classList.toggle("hidden", !items.length);
    if (!items.length) return;
    const title = document.createElement("div"); title.className = "server-queue-title";
    title.textContent = items.some(item => item.state === "interrupted") ? "有中断任务记录 · 不会自动重做" : `已排队 ${items.length} 项 · 当前任务结束后依次执行`; bar.append(title);
    for (const [index,item] of items.entries()) {
      const row = document.createElement("div"); row.className = "queue-item"; row.dataset.queueMid = item.mid;
      const text = document.createElement("div"); text.className = "q-text";
      text.textContent = `${index+1}. ${item.text || "（仅附件）"}`;
      text.title = [item.text, ...item.attachments.map(a => a.name), ...item.skills.map(s => s.name || s.id)].filter(Boolean).join("\n");
      row.append(text);
      const controls = document.createElement("div"); controls.className = "queue-actions";
      if (item.state === "interrupted") {
        text.textContent = "执行中断 · " + (item.text || "（仅附件）");
        const copy = document.createElement("button"); copy.className = "q-btn"; copy.textContent = "复制需求";
        copy.onclick = async () => {
          try { const data = await api(`/api/sessions/${encodeURIComponent(sid)}/queue?mid=${encodeURIComponent(item.mid)}`);
            const ok = await copyText(data.text); toast(ok ? "已复制，请检查项目后决定是否重新发送" : "复制失败"); }
          catch(error) { toast(error.message); }
        };
        const dismiss = document.createElement("button"); dismiss.className = "q-btn"; dismiss.textContent = "移除记录";
        dismiss.onclick = async () => { try { await change(sid,item,"cancel"); await refresh(); } catch(error) { toast(error.message); } };
        controls.append(copy,dismiss); row.append(controls); bar.append(row); continue;
      }
      const add = (label, action, disabled=false) => {
        const button = document.createElement("button"); button.type = "button"; button.className = "q-btn";
        button.textContent = label; button.disabled = disabled;
        button.onclick = async () => {
          if (action === "edit") {
            let original;
            try { original = await api(`/api/sessions/${encodeURIComponent(sid)}/queue?mid=${encodeURIComponent(item.mid)}`); }
            catch(error) { toast(error.message); await refresh(); return; }
            if (sid !== currentSession) return;
            const editingItem = {...item,revision:original.revision};
            const editVersion = ++editGeneration;
            input.value = original.text; status.textContent = ""; save.disabled = false;
            dialog.dataset.session = sid; dialog.showModal(); input.focus();
            save.onclick = async () => {
              save.disabled = cancel.disabled = true;
              try { await change(sid,editingItem,"edit",input.value);
                if (editVersion === editGeneration && sid === currentSession) dialog.close();
                await refresh(); }
              catch(error) { if (editVersion === editGeneration && sid === currentSession) status.textContent = error.message; }
              finally { if (editVersion === editGeneration) save.disabled = cancel.disabled = false; }
            };
            return;
          }
          button.disabled = true;
          try { await change(sid,item,action); }
          catch(error) { if (sid === currentSession) toast(error.message); }
          finally { await refresh(); }
        };
        controls.append(button);
      };
      if (item.reason) {
        row.title = item.reason; text.textContent = `${item.reason} · ${text.textContent}`;
        add("继续排队","resume");
      }
      add("上移","up",index===0); add("下移","down",index===items.length-1);
      add("编辑","edit",item.editable===false); add("移除","cancel"); row.append(controls); bar.append(row);
    }
  }
  async function refresh() {
    const sid = currentSession, view = sessionViewVersion, request = ++generation, token = authToken;
    if (bar.dataset.session !== (sid || "")) { bar.replaceChildren(); bar.classList.add("hidden"); bar.dataset.session = sid || ""; }
    if (dialog.open && dialog.dataset.session !== sid) dialog.close();
    if (!sid || !authToken) { bar.replaceChildren(); bar.classList.add("hidden"); return; }
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(sid)}/queue`);
      if (request !== generation || view !== sessionViewVersion || currentSession !== sid || token !== authToken) return;
      draw(data.items, sid);
    } catch(error) {
      if (request !== generation || view !== sessionViewVersion || currentSession !== sid || token !== authToken) return;
      let warning = bar.querySelector(".server-queue-status");
      if (!warning) { warning = document.createElement("div"); warning.className = "server-queue-status"; warning.setAttribute("role","status"); bar.prepend(warning); }
      warning.textContent = error.status === 404 ? "队列不可用，请刷新任务；更新代码后需重启后端。" : "队列暂时无法同步，已接收任务以服务端记录为准。";
      bar.classList.remove("hidden");
    }
  }
  setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
  return {refresh,ensureAvailable};
})();
