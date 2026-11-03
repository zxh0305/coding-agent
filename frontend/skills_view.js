/* Skill picker and editor. Skill content is always rendered as plain text. */
(() => {
  const el = id => document.getElementById(id);
  const dialog = el('skills-dialog'), picker = el('skill-picker');
  const state = window.SkillState;
  let catalog = [], selected = [], favorites = [], editing = null, dirty = false;
  let owner = who, epoch = 0, detailId = null, completion = null, activeIndex = -1;
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
  function saveDraft(sid) { persist(key(sid), selected); }
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
      tag.append(button(`✦ ${s.name || s.id}${s.scope === 'session' ? ' · 本会话' : ' · 本次'}${changed ? ' · 有更新' : ''}`, () => openDetail(s.id)),
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
    const stamp = epoch, user = who;
    if (!authToken || !user) return false;
    try {
      const data = await api('/api/skills');
      if (stamp !== epoch || user !== who) return false;
      catalog = data.skills;
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
    if (!await refresh()) note('未能加载技能，请检查连接后重试。');
  }
  function renderPicker() {
    const list = el('sp-list'), detail = el('sp-detail');
    list.replaceChildren(); detail.replaceChildren(); detail.hidden = !detailId; list.hidden = !!detailId;
    el('sp-search').hidden = !!detailId;
    if (detailId) { renderDetail(); return; }
    const query = el('sp-search').value.trim().toLowerCase();
    const items = catalog.filter(s => `${s.name} ${s.description} ${s.id}`.toLowerCase().includes(query));
    const sorted = [...items].sort((a,b) => Number(favorites.includes(b.id)) - Number(favorites.includes(a.id)));
    if (!sorted.length) {
      const empty = document.createElement('p'); empty.className = 'sk-muted';
      empty.textContent = catalog.length ? '没有匹配的技能。可以创建一个新技能。' : '还没有技能，点击下方“新建技能”开始。'; list.append(empty);
    }
    sorted.forEach(s => {
      const row = document.createElement('div'); row.className = 'sp-row';
      const chosen = selected.some(x => x.id === s.id);
      const pick = button('', () => toggle(s)); pick.className = 'sp-pick'; pick.setAttribute('aria-pressed', String(chosen));
      const title = document.createElement('strong'); title.textContent = `${chosen ? '✓' : '○'} ${s.name}`;
      const desc = document.createElement('span'); desc.textContent = s.description || s.id; pick.append(title, desc);
      const star = button(favorites.includes(s.id) ? '★' : '☆', () => favorite(s.id), `${favorites.includes(s.id) ? '取消收藏' : '收藏'} ${s.name}`);
      star.className = 'sp-star'; star.setAttribute('aria-pressed', String(favorites.includes(s.id)));
      row.append(pick, star, button('⋯', () => openDetail(s.id), `查看 ${s.name} 的选项`)); list.append(row);
    });
    positionPicker();
  }
  function renderDetail() {
    const box = el('sp-detail');
    const chosen = selected.find(s => s.id === detailId), fresh = catalog.find(s => s.id === detailId), s = chosen || fresh;
    if (!s) { detailId = null; renderPicker(); return; }
    box.append(button('← 全部技能', () => { detailId = null; renderPicker(); el('sp-search').focus(); }));
    const title = document.createElement('h3'); title.textContent = s.name || s.id; box.append(title);
    const field = document.createElement('fieldset'), legend = document.createElement('legend'); legend.textContent = '作用范围'; field.append(legend);
    for (const [scope, text] of [['once','仅下一条消息'], ['session','当前会话']]) {
      const label = document.createElement('label'), radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'skill-scope'; radio.value = scope; radio.checked = (chosen?.scope || 'once') === scope;
      radio.onchange = () => {
        if (!chosen && !choose(s)) return;
        setSelection(selected.map(x => x.id === s.id ? {...x, scope} : x));
      };
      label.append(radio, document.createTextNode(text)); field.append(label);
    }
    box.append(field);
    if (!chosen) box.append(button('选择此技能', () => choose(s)));
    const notice = document.createElement('p'); notice.className = 'sk-muted';
    if (!fresh) notice.textContent = '技能已删除，请取消选择或重新创建。';
    else if (chosen && chosen.version !== fresh.version) {
      notice.textContent = '技能库有新版本。当前选择仍保留旧版本，发送前请应用新版。';
      box.append(button('应用新版本', () => { setSelection(state.update(selected, fresh)); note('已应用新版本'); }));
    } else notice.textContent = '本次使用发送后清空；本会话使用会保留到你取消选择。';
    box.append(notice);
    const content = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
    summary.textContent = '查看当前指令'; pre.textContent = s.body || '请在管理技能中查看指令。'; content.append(summary, pre); box.append(content);
    const actions = document.createElement('div'); actions.className = 'sk-actions';
    if (fresh) actions.append(button('编辑技能', () => openManager(fresh.id)), button('复制技能', () => { openManager(null, {...fresh, id:'', name:fresh.name + ' 副本'}); }));
    if (chosen) actions.append(button('取消选择', () => { setSelection(selected.filter(x => x.id !== s.id)); closePicker(); }));
    box.append(actions); positionPicker();
  }
  async function openDetail(id) {
    completion = null; detailId = id; note(''); positionPicker();
    if (!picker.matches(':popover-open')) picker.showPopover(); renderPicker(); await refresh();
  }
  function canLeave() { return !dirty || confirm('有未保存的技能修改，放弃这些修改吗？'); }
  function edit(skill, isNew = false) {
    editing = !isNew && skill && catalog.some(s => s.id === skill.id) ? skill.id : null;
    el('sk-empty').hidden = true; el('sk-form').hidden = false;
    el('sk-id').value = isNew ? '' : skill?.id || ''; el('sk-id').readOnly = !!editing;
    el('sk-name').value = skill?.name || ''; el('sk-desc').value = skill?.description || ''; el('sk-body').value = skill?.body || '';
    el('sk-advanced').open = false; el('sk-delete').hidden = !editing; el('sk-select').disabled = false;
    dirty = !!skill && !editing; status(''); renderList();
  }
  function renderList() {
    const list = el('sk-list'); list.replaceChildren(); const query = el('sk-search').value.toLowerCase();
    const items = catalog.filter(s => `${s.name} ${s.description} ${s.id}`.toLowerCase().includes(query));
    if (!items.length) { const p = document.createElement('p'); p.className = 'sk-muted'; p.textContent = catalog.length ? '没有匹配的技能' : '还没有技能，点击上方新建。'; list.append(p); }
    for (const s of items) {
      const row = document.createElement('div'); row.className = 'sk-manage-row';
      const item = button('', () => { if (canLeave()) edit(s); }); item.className = 'sk-row' + (editing === s.id ? ' active' : '');
      const name = document.createElement('strong'), desc = document.createElement('span'); name.textContent = s.name; desc.textContent = s.description || s.id;
      item.append(name, desc); row.append(item, button(favorites.includes(s.id) ? '★' : '☆', () => favorite(s.id), `收藏 ${s.name}`)); list.append(row);
    }
  }
  async function openManager(id, draft) {
    closePicker();
    // Prepare the form before exposing it: a slow refresh must not overwrite typing.
    const stamp = epoch;
    if (!await refresh() || stamp !== epoch) { if (stamp === epoch) toast('加载失败，请重试。', 'error'); return; }
    if ((draft || id !== undefined) && !canLeave()) return;
    if (draft) edit(draft, true);
    else if (id) { const s = catalog.find(s => s.id === id); if (s) edit(s); }
    else if (id === null) edit(null);
    if (!dialog.open) dialog.showModal();
  }
  async function save(use) {
    if (!el('sk-form').reportValidity()) return;
    const stamp = epoch;
    el('sk-select').disabled = true;
    try {
      const id = el('sk-id').value.trim() || state.autoId(el('sk-name').value, catalog.map(s => s.id));
      if (!editing && catalog.some(s => s.id === id)) throw new Error('这个标识已经存在，请更换标识或从列表打开它进行编辑');
      const data = await api('/api/skills', {method:'POST', body:JSON.stringify({id,name:el('sk-name').value,description:el('sk-desc').value,body:el('sk-body').value})});
      if (stamp !== epoch) return;
      catalog = catalog.filter(s => s.id !== data.skill.id).concat(data.skill);
      edit(data.skill); renderSelection();
      const old = selected.find(s => s.id === id);
      status(old && old.version !== data.skill.version ? '已保存到技能库。当前选择保持原版本，可在技能标签中应用新版。' : '已保存到技能库');
      if (use && choose(data.skill)) { dialog.close(); inputEl.focus(); }
    } catch(e) { status(e.message); }
    finally { el('sk-select').disabled = false; }
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
      const id = editing; await api('/api/skills?id=' + encodeURIComponent(id), {method:'DELETE'});
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
    selection, setSelection, saveDraft, restoreDraft, refresh,
    afterSend() { setSelection(state.afterSend(selected)); closePicker(); },
    adoptSession(sid) { saveDraft(sid); try { localStorage.removeItem(key(null)); } catch {} },
    reset() {
      epoch++; owner = ''; selected = []; catalog = []; favorites = []; editing = null; dirty = false;
      closePicker(); dialog.close(); el('sk-form').reset(); el('sk-form').hidden = true; el('sk-empty').hidden = false; renderSelection();
    }
  };
  restoreDraft(currentSession); refresh();
})();
