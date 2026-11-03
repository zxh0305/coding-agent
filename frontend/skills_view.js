/* Skills panel: text-only DOM rendering; no skill content is evaluated. */
(() => {
  const el = id => document.getElementById(id);
  const dialog = el("skills-dialog");
  let catalog = [], selected = [], editing = null, dirty = false;
  const key = sid => `skill_draft_${who || "user"}_${sid || "new"}`;
  const status = text => { el("sk-status").textContent = text; };
  function selection() { return selected.map(s => ({id:s.id, version:s.version, name:s.name, body:s.body || ""})); }
  function setSelection(items) {
    selected = (items || []).map(s => ({...s}));
    const tray = el("skill-tray"); tray.replaceChildren();
    tray.classList.toggle("hidden", !selected.length);
    selected.forEach(s => {
      const tag = document.createElement("span"); tag.className = "skill-tag";
      const view = document.createElement("button"); view.type = "button"; view.textContent = "✦ " + (s.name || s.id); view.onclick = () => open(s.id);
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.setAttribute("aria-label", "取消选择 " + (s.name || s.id)); remove.onclick = () => setSelection(selected.filter(x => x.id !== s.id));
      tag.append(view, remove); tray.append(tag);
    });
    el("skills-count").textContent = selected.length ? String(selected.length) : "";
    saveDraft(currentSession);
  }
  function saveDraft(sid) { try { localStorage.setItem(key(sid), JSON.stringify(selection())); } catch {} }
  function restoreDraft(sid) { let items = []; try { items = JSON.parse(localStorage.getItem(key(sid)) || "[]"); } catch {} setSelection(Array.isArray(items) ? items : []); }
  function canLeave() { return !dirty || confirm("有未保存的技能修改，放弃这些修改吗？"); }
  function edit(skill) {
    editing = skill?.id || null;
    el("sk-empty").hidden = true; el("sk-form").hidden = false;
    el("sk-id").value = skill?.id || ""; el("sk-id").readOnly = !!editing && catalog.some(s => s.id === editing);
    el("sk-name").value = skill?.name || ""; el("sk-desc").value = skill?.description || ""; el("sk-body").value = skill?.body || "";
    el("sk-delete").hidden = !editing; el("sk-select").disabled = !editing;
    dirty = false; status(""); renderList();
  }
  function renderList() {
    const list = el("sk-list"); list.replaceChildren();
    const query = el("sk-search").value.toLowerCase();
    const items = catalog.filter(s => (s.name + " " + s.description + " " + s.id).toLowerCase().includes(query));
    if (!items.length) { const p = document.createElement("p"); p.className = "sk-muted"; p.textContent = catalog.length ? "没有匹配的技能" : "还没有技能，点击上方新建。"; list.append(p); }
    items.forEach(s => {
      const row = document.createElement("button"); row.type = "button"; row.className = "sk-row" + (editing === s.id ? " active" : "");
      const title = document.createElement("strong"); title.textContent = s.name;
      const desc = document.createElement("span"); desc.textContent = s.description || s.id;
      row.append(title, desc); row.onclick = () => { if (canLeave()) edit(s); }; list.append(row);
    });
  }
  async function open(id) {
    try {
      catalog = (await api("/api/skills")).skills;
      if (!dialog.open) dialog.showModal();
      if (id && canLeave()) { const s = catalog.find(s => s.id === id); if (s) edit(s); }
      renderList();
    } catch(e) { toast(e.message, "error"); }
  }
  el("skills-chip").onclick = () => open();
  el("sk-close").onclick = () => { if (canLeave()) { dirty = false; dialog.close(); } };
  dialog.addEventListener("cancel", e => { if (!canLeave()) e.preventDefault(); else dirty = false; });
  el("sk-new").onclick = () => { if (canLeave()) edit(null); };
  el("sk-example").onclick = () => edit({id:catalog.some(s => s.id === "code-review") ? "code-review-" + Date.now().toString(36) : "code-review",name:"代码审查",description:"检查代码中的正确性、边界条件和可维护性问题。",body:"阅读相关代码和上下文，优先找出会影响实际行为的问题。\n逐项说明问题、触发条件、影响和修改建议，并标注文件位置。\n按严重程度排序，不编造未运行的测试结果。如果没有发现问题，明确说明检查范围与验证限制。"});
  el("sk-search").oninput = renderList;
  el("sk-form").oninput = () => { dirty = true; el("sk-select").disabled = true; status("有未保存的修改 · 保存后可使用"); };
  el("sk-form").onsubmit = async e => {
    e.preventDefault();
    try {
      if (!editing && catalog.some(s => s.id === el("sk-id").value.trim())) throw new Error("这个标识已经存在，请更换标识或从列表打开它进行编辑");
      const data = await api("/api/skills", {method:"POST",body:JSON.stringify({id:el("sk-id").value.trim(),name:el("sk-name").value,description:el("sk-desc").value,body:el("sk-body").value})});
      catalog = catalog.filter(s => s.id !== data.skill.id).concat(data.skill);
      if (selected.some(s => s.id === data.skill.id)) setSelection(selected.map(s => s.id === data.skill.id ? data.skill : s));
      edit(data.skill); status("已保存");
    } catch(e) { status(e.message); }
  };
  el("sk-select").onclick = () => {
    const skill = catalog.find(s => s.id === editing); if (!skill) return;
    if (selected.some(s => s.id === skill.id)) setSelection(selected.map(s => s.id === skill.id ? skill : s));
    else { if (selected.length >= 6) { status("每条消息最多选择6个技能"); return; } setSelection([...selected, skill]); }
    dialog.close(); inputEl.focus();
  };
  el("sk-delete").onclick = async () => {
    if (!editing || !confirm("删除这个技能？历史消息中保存的版本仍会保留。")) return;
    try {
      await api("/api/skills?id=" + encodeURIComponent(editing), {method:"DELETE"});
      catalog = catalog.filter(s => s.id !== editing); setSelection(selected.filter(s => s.id !== editing));
      editing = null; dirty = false; el("sk-form").hidden = true; el("sk-empty").hidden = false; status("已删除"); renderList();
    } catch(e) { status(e.message); }
  };
  el("sk-import").onclick = () => { if (canLeave()) el("sk-file").click(); };
  el("sk-file").onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    try {
      if (file.size > 48000) throw new Error("文件超过48KB");
      let body = await file.text(), name = file.name.replace(/\.md$/i, ""), description = "";
      const match = body.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/);
      if (match) {
        for (const line of match[1].split(/\r?\n/)) {
          const m = line.match(/^(name|description):\s*(.*)$/); if (!m) continue;
          let value = m[2]; try { value = JSON.parse(value); } catch { value = value.replace(/^['"]|['"]$/g, ""); }
          if (m[1] === "name") name = String(value); else description = String(value);
        }
        body = body.slice(match[0].length);
      }
      edit(null); el("sk-id").value = /^[a-z0-9][a-z0-9-]{0,63}$/.test(name) ? name : "";
      el("sk-name").value = name; el("sk-desc").value = description; el("sk-body").value = body.trim(); dirty = true;
      status("已导入草稿，请检查标识与内容后保存。当前支持单个 SKILL.md 指令文件。");
    } catch(err) { status(err.message); }
    e.target.value = "";
  };
  window.SkillUI = {selection,setSelection,saveDraft,restoreDraft, reset() { selected = []; catalog = []; editing = null; dirty = false; dialog.close(); el("skill-tray").replaceChildren(); el("skill-tray").classList.add("hidden"); el("skills-count").textContent = ""; el("sk-form").reset(); el("sk-form").hidden = true; el("sk-empty").hidden = false; }};
  restoreDraft(currentSession);
})();
