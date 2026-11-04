/* Skill picker and editor. Skill content is always rendered as plain text. */
(() => {
  const el = id => document.getElementById(id);
  const dialog = el('skills-dialog'), picker = el('skill-picker');
  const state = window.SkillState;
  let catalog = [], selected = [], favorites = [], editing = null, dirty = false;
  let owner = who, epoch = 0, detailId = null, completion = null, activeIndex = -1;
  let context = null, request = 0, editVersion = null;
  let defaultIds = [], draftProject = '', defaultsReady = true;
  const tabDraftKey = () => 'skill_project_draft_' + who;
  function rememberProjectDraft() {
    if (!who || currentSession) return;
    try {
      if (draftProject) sessionStorage.setItem(tabDraftKey(), JSON.stringify({project:draftProject, ready:defaultsReady, selected}));
      else sessionStorage.removeItem(tabDraftKey());
    } catch { toast('当前标签页无法保存项目草稿'); }
  }
  function startProject(project) {
    context = null;
    draftProject = project || '';
    defaultsReady = !draftProject;
    selected = selected.filter(s => !s.fromDefault && !s.id.startsWith('project:'));
    saveDraft(null);
  }
  function resumeProject() {
    restoreDraft(null);
    let draft;
    try { draft = JSON.parse(sessionStorage.getItem(tabDraftKey())); } catch {}
    draftProject = draft?.project || '';
    defaultsReady = !draftProject || !!draft.ready;
    if (draftProject) selected = state.normalize(draft.selected);
    return draftProject || null;
  }
  const projectPath = () => wsCustom ? el('ws-pick').title : '';
  function skillUrl(extra = {}) {
    const q = new URLSearchParams(extra);
    if (currentSession) q.set('session_id', currentSession);
    if (projectPath()) q.set('workspace', projectPath());
    return '/api/skills?' + q;
  }
  function changeContext() {
    if (!currentSession && draftProject !== projectPath()) startProject(projectPath());
    const next = JSON.stringify([who, currentSession, projectPath(), workspacePending]);
    if (next === context) return;
    context = next; epoch++; catalog = []; detailId = null; editing = null;
    closePicker(); dialog.close(); dirty = false;
    el('sk-form').hidden = true; el('sk-empty').hidden = false;
    renderSelection(); renderList();
    if (!workspacePending) refresh();
  }
  async function withBody(skill, action) {
    const stamp = epoch;
    try {
      if (skill.body === undefined) {
        const {skill: full} = await api(skillUrl({id:skill.id}));
        if (stamp !== epoch) return;
        Object.assign(skill, full);
      }
      if (stamp === epoch) action(skill);
    } catch(e) { if (stamp === epoch) toast(e.message, 'error'); }
  }
  const key = sid => `skill_draft_${who || 'user'}_${sid || 'new'}`;
  const favoriteKey = () => `skill_favorites_${who || 'user'}`;
  const status = text => { el('sk-status').textContent = text; };
  const note = text => { el('sp-status').textContent = text; };
  function readJSON(k, fallback) { try { return JSON.parse(localStorage.getItem(k)) ?? fallback; } catch { return fallback; } }
  function persist(k, value) { try { localStorage.setItem(k, JSON.stringify(value)); } catch { toast('浏览器存储空间不足，技能选择仅保留在当前页面', 'error'); } }
  const button = (text, action, label) => {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = text;
    if (label) b.setAttribute('aria-label', label);
    b.onclick = action; return b;
  };
  function favoriteButton(skill) {
    const saved = favorites.includes(skill.id);
    const b = button('', () => favorite(skill.id), `${saved ? '取消收藏' : '收藏'} ${skill.name}`);
    b.className = 'sk-favorite'; b.setAttribute('aria-pressed', String(saved));
    b.title = saved ? '取消收藏' : '收藏到常用技能';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm12 3 2.78 5.63L21 9.54l-4.5 4.39 1.06 6.2L12 17.2l-5.56 2.93 1.06-6.2L3 9.54l6.22-.91Z');
    svg.append(path); b.append(svg); return b;
  }
  function saveDraft(sid) { persist(key(sid), selected); if (!sid) rememberProjectDraft(); }
  function restoreDraft(sid) {
    selected = state.normalize(readJSON(key(sid), []));
    renderSelection();
  }
  function setSelection(items) {
    selected = state.normalize(items); saveDraft(currentSession); renderSelection();
  }
  function selection() { return state.snapshot(selected); }
  function renderSelection() {
    const tray = el('skill-tray'); tray.replaceChildren(); tray.classList.toggle('hidden', !selected.length);
    for (const s of selected) {
      const tag = document.createElement('span'); tag.className = 'skill-tag';
      const fresh = catalog.find(x => x.id === s.id);
      const changed = fresh && fresh.version !== s.version;
      tag.append(button(`✦ ${s.name || s.id}${s.fromDefault ? ' · 项目默认' : s.scope === 'session' ? ' · 本会话' : ' · 本次'}${changed ? ' · 有更新' : ''}`, () => openDetail(s.id)),
        button('×', () => setSelection(selected.filter(x => x.id !== s.id)), `取消选择 ${s.name || s.id}`));
      tray.append(tag);
    }
    el('skills-count').textContent = selected.length || '';
    renderFavorites();
    if (picker.matches(':popover-open')) renderPicker();
  }
  function renderFavorites() {
    const box = el('skill-favorites'); box.replaceChildren();
    const items = favorites.map(id => catalog.find(s => s.id === id)).filter(Boolean);
    box.classList.toggle('hidden', !items.length);
    if (items.length) { const label = document.createElement('span'); label.textContent = '常用'; box.append(label); }
    for (const s of items.slice(0, 3)) {
      const chosen = selected.some(x => x.id === s.id);
      const b = button(`${chosen ? '✓' : '✦'} ${s.name}`, () => toggle(s));
      b.setAttribute('aria-pressed', String(chosen)); b.title = s.description || s.name; box.append(b);
    }
    if (items.length > 3) box.append(button(`更多 ${items.length - 3} →`, () => openPicker()));
  }
  async function refresh() {
    const stamp = epoch, user = who, seq = ++request;
    if (!authToken || !user || workspacePending) return false;
    try {
      const initialize = !currentSession && !defaultsReady && draftProject === projectPath();
      const data = await api(skillUrl({summary:'1', ...(initialize ? {initialize:'1'} : {})}));
      if (stamp !== epoch || user !== who || seq !== request) return false;
      note((data.diagnostics || []).join('；'));
      catalog = data.skills.map(s => catalog.find(old => old.id === s.id && old.version === s.version && old.body !== undefined) || s);
      defaultIds = data.defaults || [];
      const compatible = selected.filter(s => !s.id.startsWith('project:') || (data.project_prefix && s.id.startsWith(data.project_prefix)));
      if (compatible.length !== selected.length) { selected = compatible; saveDraft(currentSession); }
      if (initialize && !defaultsReady) {
        const defaults = defaultIds.map(id => catalog.find(s => s.id === id)).filter(Boolean)
          .map(s => ({...s, scope:'session', fromDefault:true}));
        defaultsReady = true;
        setSelection([...compatible, ...defaults]);
      }
      favorites = readJSON(favoriteKey(), []);
      if (!Array.isArray(favorites)) favorites = [];
      if (owner !== who) { owner = who; restoreDraft(currentSession); }
      renderSelection(); renderList(); return true;
    } catch(e) { if (stamp === epoch && user === who) note(e.message); return false; }
  }
  function favorite(id) {
    favorites = favorites.includes(id) ? favorites.filter(x => x !== id) : [...favorites, id];
    persist(favoriteKey(), favorites); renderFavorites(); renderPicker(); renderList();
  }
  function choose(skill) {
    if (!selected.some(s => s.id === skill.id) && selected.length >= 6) { note('每条消息最多选择6个技能'); toast('每条消息最多选择6个技能', 'error'); return false; }
    setSelection(selected.some(s => s.id === skill.id) ? state.update(selected, skill) : [...selected, {...skill, scope:'once'}]);
    return true;
  }
  function toggle(skill) {
    if (skill.body === undefined) {
      const completing = completion;
      const input = inputEl.value;
      return withBody(skill, full => {
        if (completion === completing && (!completing || inputEl.value === input)) toggle(full);
      });
    }
    if (completion) { if (!choose(skill)) return; }
    else if (selected.some(s => s.id === skill.id)) setSelection(selected.filter(s => s.id !== skill.id));
    else if (!choose(skill)) return;
    if (completion) {
      const {start, end} = completion;
      inputEl.setRangeText('', start, end, 'end'); inputEl.dispatchEvent(new Event('input', {bubbles:true}));
      closePicker(); inputEl.focus();
    }
  }
  function positionPicker() {
    const rect = el('skills-chip').getBoundingClientRect();
    picker.style.width = `${Math.min(410, innerWidth - 24)}px`;
    picker.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - Math.min(410, innerWidth - 24) - 12))}px`;
    picker.style.bottom = `${Math.max(12, innerHeight - rect.top + 8)}px`;
    picker.style.maxHeight = `${Math.max(200, rect.top - 24)}px`;
  }
  function closePicker() { if (picker.matches(':popover-open')) picker.hidePopover(); completion = null; }
  async function openPicker(query = '', keepInput = false) {
    detailId = null; activeIndex = -1; el('sp-search').value = query; note('');
    positionPicker(); if (!picker.matches(':popover-open')) picker.showPopover(); renderPicker();
    if (!keepInput) el('sp-search').focus();
    if (!keepInput || !catalog.length) await refresh();
  }
  function renderPicker() {
    const list = el('sp-list'), detail = el('sp-detail');
    picker.querySelector('header strong').textContent = detailId ? '技能设置' : '选择技能';
    list.replaceChildren(); detail.replaceChildren(); detail.hidden = !detailId; list.hidden = !!detailId;
    el('sp-search').hidden = !!detailId;
    if (detailId) { renderDetail(); return; }
    const query = el('sp-search').value.trim().toLowerCase();
    const items = catalog.filter(s => `${s.name} ${s.description} ${s.id}`.toLowerCase().includes(query));
    const sorted = [...items].sort((a,b) => Number(b.source === "project") - Number(a.source === "project") || Number(favorites.includes(b.id)) - Number(favorites.includes(a.id)));
    if (!sorted.length) {
      const empty = document.createElement('p'); empty.className = 'sk-muted';
      empty.textContent = catalog.length ? '没有匹配的技能。可以创建一个新技能。' : '还没有技能，点击下方“新建技能”开始。'; list.append(empty);
    }
    for (const id of defaultIds.filter(id => !catalog.some(s => s.id === id))) {
      const remove = button('移除失效的默认技能 · ' + id.split(':').pop(), () => changeDefault(id, false, remove));
      list.append(remove);
    }
    let group;
    sorted.forEach(s => {
      if (s.source !== group) {
        group = s.source;
        const label = document.createElement('small');
        label.className = 'sk-muted'; label.textContent = group === 'project' ? '当前项目' : '个人通用';
        list.append(label);
      }
      const row = document.createElement('div'); row.className = 'sp-row';
      const chosen = selected.some(x => x.id === s.id);
      const pick = button('', () => toggle(s)); pick.className = 'sp-pick'; pick.setAttribute('aria-pressed', String(chosen));
      const title = document.createElement('strong'); title.textContent = `${chosen ? '✓' : '○'} ${s.name}`;
      const desc = document.createElement('span'); desc.textContent = s.description || s.slug; pick.append(title, desc);
      const star = favoriteButton(s); star.classList.add('sp-star');
      row.append(pick, star, button('⋯', () => openDetail(s.id), `查看 ${s.name} 的选项`)); list.append(row);
    });
    positionPicker();
  }
  async function changeDefault(id, enabled, control) {
    const stamp = epoch;
    control.disabled = true;
    try {
      const data = await api(skillUrl({defaults:'1'}), {method:'POST', body:JSON.stringify({id, enabled})});
      if (stamp !== epoch) return;
      request++; defaultIds = data.defaults; renderPicker();
      note(enabled ? '已设为此项目新任务的默认技能' : '已取消项目默认');
    } catch(e) {
      if (stamp === epoch) { renderPicker(); note(e.message); }
    }
  }
  function renderDetail() {
    const box = el('sp-detail');
    const chosen = selected.find(s => s.id === detailId), fresh = catalog.find(s => s.id === detailId), s = chosen || fresh;
    if (!s) { detailId = null; renderPicker(); return; }
    const back = button('← 全部技能', () => { detailId = null; renderPicker(); el('sp-search').focus(); });
    back.className = 'sp-back'; box.append(back);
    const title = document.createElement('h3'); title.textContent = s.name || s.id; box.append(title);
    const field = document.createElement('fieldset'), legend = document.createElement('legend'); legend.textContent = '作用范围'; field.append(legend);
    for (const [scope, text] of [['once','仅下一条消息'], ['session','当前会话']]) {
      const label = document.createElement('label'), radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'skill-scope'; radio.value = scope; radio.checked = (chosen?.scope || 'once') === scope;
      radio.onchange = () => withBody(s, full => {
        if (!chosen && !choose(full)) return;
        setSelection(selected.map(x => x.id === s.id ? {...x, scope, fromDefault:false} : x));
      });
      label.append(radio, document.createTextNode(text)); field.append(label);
    }
    box.append(field);
    if (fresh && projectPath()) {
      const label = document.createElement('label'), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = defaultIds.includes(s.id);
      check.onchange = () => changeDefault(s.id, check.checked, check);
      label.append(check, document.createTextNode(' 在此项目的新任务中默认使用'));
      box.append(label);
    }
    if (!chosen) box.append(button('选择此技能', () => withBody(s, choose)));
    const notice = document.createElement('p'); notice.className = 'sk-muted sp-scope-help';
    if (!fresh) notice.textContent = '技能已删除，请取消选择或重新创建。';
    else if (chosen && chosen.version !== fresh.version) {
      notice.textContent = '技能库有新版本。当前选择仍保留旧版本，发送前请应用新版。';
      box.append(button('应用新版本', () => { withBody(fresh, full => { setSelection(state.update(selected, full)); note('已应用新版本'); }); }));
    } else notice.textContent = chosen?.scope === 'session' ? '本会话后续消息持续使用，直到取消选择。' : '仅用于下一条消息，发送后自动清空。';
    box.append(notice);
    const content = document.createElement('section'); content.className = 'sp-instruction';
    const heading = document.createElement('div'); heading.className = 'sp-instruction-heading';
    const label = document.createElement('strong'); label.textContent = '技能指令';
    const source = document.createElement('span'); source.textContent = chosen ? '当前选择的版本' : '技能库版本'; heading.append(label, source);
    const pre = document.createElement('pre'); pre.tabIndex = 0; pre.setAttribute('aria-label', '技能指令');
    pre.textContent = s.body || '请在管理技能中查看指令。'; content.append(heading, pre); box.append(content);
    const actions = document.createElement('div'); actions.className = 'sp-detail-actions';
    if (fresh) actions.append(button('编辑技能', () => openManager(fresh.id)), button('复制技能', () => withBody(fresh, full => openManager(null, {...full, id:'', name:full.name + ' 副本'}))));
    if (chosen) actions.append(button('取消选择', () => { setSelection(selected.filter(x => x.id !== s.id)); closePicker(); }));
    box.append(actions); positionPicker();
  }
  async function openDetail(id) {
    completion = null; detailId = id; note(''); positionPicker();
    if (!picker.matches(':popover-open')) picker.showPopover();
    renderPicker();
    await refresh();
    const fresh = catalog.find(s => s.id === id);
    if (detailId === id && fresh) await withBody(fresh, () => { if (detailId === id) renderPicker(); });
  }
  function canLeave() { return !dirty || confirm('有未保存的技能修改，放弃这些修改吗？'); }
  function edit(skill, isNew = false) {
    editing = !isNew && skill && catalog.some(s => s.id === skill.id) ? skill.id : null;
    el('sk-empty').hidden = true; el('sk-form').hidden = false;
    editVersion = editing ? skill.version : '';
    el('sk-source').value = editing ? skill.source : (projectPath() ? 'project' : 'personal');
    el('sk-source').disabled = !!editing || !projectPath();
    el('sk-id').value = isNew ? '' : skill?.slug || skill?.id || ''; el('sk-id').readOnly = !!editing;
    el('sk-name').value = skill?.name || ''; el('sk-desc').value = skill?.description || ''; el('sk-body').value = skill?.body || '';
    el('sk-advanced').open = false; el('sk-delete').hidden = !editing; el('sk-select').disabled = false;
    dirty = !!skill && !editing; status(skill?.editable === false ? '此技能有扩展元数据，请在项目原文件中编辑。' : ''); renderList();
  }
  function renderList() {
    const list = el('sk-list'); list.replaceChildren(); const query = el('sk-search').value.toLowerCase();
    const items = catalog.filter(s => `${s.name} ${s.description} ${s.id}`.toLowerCase().includes(query));
    if (!items.length) { const p = document.createElement('p'); p.className = 'sk-muted'; p.textContent = catalog.length ? '没有匹配的技能' : '还没有技能，点击上方新建。'; list.append(p); }
    for (const s of items) {
      const row = document.createElement('div'); row.className = 'sk-manage-row' + (editing === s.id ? ' active' : '');
      const item = button('', () => { if (canLeave()) withBody(s, edit); }); item.className = 'sk-row' + (editing === s.id ? ' active' : '');
      const name = document.createElement('strong'), desc = document.createElement('span'); name.textContent = s.name; desc.textContent = (s.source === 'project' ? '当前项目' : '个人通用') + (s.description ? ' · ' + s.description : '');
      item.title = s.description || s.name;
      item.append(name, desc); row.append(item, favoriteButton(s)); list.append(row);
    }
  }
  async function openManager(id, draft) {
    closePicker();
    // Prepare the form before exposing it: a slow refresh must not overwrite typing.
    const stamp = epoch;
    if (!await refresh() || stamp !== epoch) { if (stamp === epoch) toast('加载失败，请重试。', 'error'); return; }
    if ((draft || id !== undefined) && !canLeave()) return;
    if (draft) edit(draft, true);
    else if (id) { const s = catalog.find(s => s.id === id); if (s) await withBody(s, edit); }
    else if (id === null) edit(null);
    if (stamp === epoch && !dialog.open) dialog.showModal();
  }
  async function save(use) {
    if (!el('sk-form').reportValidity()) return;
    const stamp = epoch;
    el('sk-select').disabled = true;
    try {
      const source = el('sk-source').value;
      const slugs = catalog.filter(s => s.source === source).map(s => s.slug);
      const id = el('sk-id').value.trim() || state.autoId(el('sk-name').value, slugs);
      if (!editing && slugs.includes(id)) throw new Error('这个标识已经存在，请更换标识或从列表打开它进行编辑');
      const data = await api(skillUrl(), {method:'POST', body:JSON.stringify({id:editing || id,source,expected_version:editVersion,name:el('sk-name').value,description:el('sk-desc').value,body:el('sk-body').value})});
      if (stamp !== epoch) return;
      request++;
      catalog = catalog.filter(s => s.id !== data.skill.id).concat(data.skill);
      edit(data.skill); renderSelection();
      const old = selected.find(s => s.id === data.skill.id);
      status(old && old.version !== data.skill.version ? '已保存到技能库。当前选择保持原版本，可在技能标签中应用新版。' : '已保存到技能库');
      if (use && choose(data.skill)) { dialog.close(); inputEl.focus(); }
    } catch(e) { if (stamp === epoch) status(e.message); }
    finally { if (stamp === epoch) el('sk-select').disabled = false; }
  }
  el('skills-chip').onclick = () => picker.matches(':popover-open') ? closePicker() : openPicker();
  el('sp-close').onclick = closePicker;
  picker.addEventListener('toggle', e => {
    el('skills-chip').setAttribute('aria-expanded', String(e.newState === 'open'));
    if (e.newState === 'closed') completion = null;
  });
  el('sp-search').oninput = () => { activeIndex = -1; renderPicker(); };
  el('sp-new').onclick = () => openManager(null);
  el('sp-manage').onclick = () => openManager();
  el('sk-close').onclick = () => { if (canLeave()) { dirty = false; dialog.close(); } };
  dialog.addEventListener('cancel', e => { if (!canLeave()) e.preventDefault(); else dirty = false; });
  el('sk-new').onclick = () => { if (canLeave()) edit(null); };
  el('sk-example').onclick = () => edit({name:'代码审查', description:'检查代码中的正确性、边界条件和可维护性问题。', body:'阅读相关代码和上下文，优先找出会影响实际行为的问题。\n逐项说明问题、触发条件、影响和修改建议，并标注文件位置。\n按严重程度排序，不编造未运行的测试结果。如果没有发现问题，明确说明检查范围与验证限制。'}, true);
  el('sk-search').oninput = renderList;
  el('sk-form').oninput = () => { dirty = true; status('有未保存的修改'); };
  el('sk-form').onsubmit = e => { e.preventDefault(); save(false); };
  el('sk-select').onclick = () => save(true);
  el('sk-delete').onclick = async () => {
    if (!editing || !confirm('删除这个技能？历史消息中保存的版本仍会保留。')) return;
    try {
      const id = editing, stamp = epoch; await api(skillUrl({id,expected_version:editVersion}), {method:'DELETE'});
      if (stamp !== epoch) return;
      request++;
      catalog = catalog.filter(s => s.id !== id); favorites = favorites.filter(x => x !== id); persist(favoriteKey(), favorites);
      setSelection(selected.filter(s => s.id !== id)); editing = null; dirty = false;
      el('sk-form').hidden = true; el('sk-empty').hidden = false; status('已删除'); renderList();
    } catch(e) { status(e.message); }
  };
  el('sk-import').onclick = () => { if (canLeave()) el('sk-file').click(); };
  el('sk-file').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    try {
      if (file.size > 48000) throw new Error('文件超过48KB');
      let body = await file.text(), name = file.name.replace(/\.md$/i,''), description = '';
      const match = body.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/);
      if (match) {
        for (const line of match[1].split(/\r?\n/)) {
          const m = line.match(/^(name|description):\s*(.*)$/); if (!m) continue;
          let value = m[2]; try { value = JSON.parse(value); } catch { value = value.replace(/^['"]|['"]$/g,''); }
          if (m[1] === 'name') name = String(value); else description = String(value);
        }
        body = body.slice(match[0].length);
      }
      edit({name,description,body:body.trim()}, true); status('已导入草稿，检查后保存即可。当前支持单个 SKILL.md 指令文件。');
    } catch(err) { status(err.message); }
    e.target.value = '';
  };
  inputEl.addEventListener('input', e => {
    if (e.isComposing) return;
    const end = inputEl.selectionStart, before = inputEl.value.slice(0, end), match = before.match(/(?:^|\s)\$([^\s$]*)$/);
    if (!match) { if (completion) closePicker(); return; }
    completion = {start:end - match[1].length - 1, end}; openPicker(match[1], true);
  });
  function navigation(e) {
    if (e.isComposing || e.keyCode === 229 || !picker.matches(':popover-open') || detailId) return;
    const items = [...el('sp-list').querySelectorAll('.sp-pick')];
    if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); closePicker(); inputEl.focus(); }
    else if (['ArrowDown','ArrowUp'].includes(e.key) && items.length) {
      e.preventDefault(); e.stopImmediatePropagation(); activeIndex = (activeIndex + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items.forEach((b,i) => b.classList.toggle('keyboard-active', i === activeIndex)); items[activeIndex].scrollIntoView({block:'nearest'});
    } else if (e.key === 'Enter' && !e.shiftKey && (completion || activeIndex >= 0)) {
      e.preventDefault(); e.stopImmediatePropagation(); items[Math.max(0, activeIndex)]?.click();
    }
  }
  inputEl.addEventListener('keydown', navigation, true); el('sp-search').addEventListener('keydown', navigation);
  window.addEventListener('resize', () => { if (picker.matches(':popover-open')) positionPicker(); });
  window.SkillUI = {
    selection, setSelection, saveDraft, restoreDraft, refresh, changeContext, startProject, resumeProject,
    readyToSend() {
      if (currentSession || defaultsReady) return true;
      toast('正在确认项目默认技能，请稍后发送');
      refresh();
      return false;
    },
    fromMessage(body) { return openManager(null, {body}); },
    afterSend() { setSelection(state.afterSend(selected)); closePicker(); },
    adoptSession(sid) { saveDraft(sid); draftProject = ''; defaultsReady = true; try { localStorage.removeItem(key(null)); sessionStorage.removeItem(tabDraftKey()); } catch {} },
    reset() {
      epoch++; owner = ''; defaultIds = []; draftProject = ''; defaultsReady = true; selected = []; catalog = []; favorites = []; editing = null; dirty = false;
      closePicker(); dialog.close(); el('sk-form').reset(); el('sk-form').hidden = true; el('sk-empty').hidden = false; renderSelection();
    }
  };
  if (!currentSession) pendingProjectPath = resumeProject();
  if (!el('layout').classList.contains('hidden')) loadWorkspace();
})();
