/* SQL 控制台补全弹层：纯逻辑来自 sql_completion.js，这里只负责渲染、定位与按键。
   弹层挂到 body 并用 fixed 定位，避免被编辑器的 overflow:hidden 裁掉。 */
(() => {
  const C = window.SqlCompletion;
  if (!C) return;
  const MAX_WORDS = 10;
  const TYPING = '↑↓ 选择 · Enter 或 Tab 插入 · Esc 关闭';

  // 光标坐标：隐藏镜像使用与 textarea 相同的字体和换行规则。
  function caretPoint(textarea, position) {
    const style = getComputedStyle(textarea);
    const mirror = document.createElement('div');
    for (const prop of ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'tabSize',
      'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderRightWidth',
      'borderBottomWidth', 'borderLeftWidth', 'boxSizing']) mirror.style[prop] = style[prop];
    Object.assign(mirror.style, { position: 'fixed', visibility: 'hidden', top: '0', left: '-9999px',
      whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', width: textarea.clientWidth + 'px', height: 'auto' });
    mirror.textContent = textarea.value.slice(0, position);
    const marker = document.createElement('span');
    marker.textContent = textarea.value.slice(position) || '.';
    mirror.append(marker);
    document.body.append(mirror);
    const size = parseFloat(style.lineHeight) || 16;
    const point = { left: marker.offsetLeft, lineHeight: size };
    mirror.remove();
    return point;
  }

  /** 绑定到 textarea；getContext() 返回 {columns:[], tables:[]}。返回控制器供测试使用。
   *  options.getContext      -> {columns, tables, notReady}
   *  options.tablesFrom(text) -> 由调用方按真实光标位置读表名（不再依赖补全侧的上下文推断） */
  function attach(textarea, options) {
    const getContext = options && options.getContext ? options.getContext : () => ({});
    const pop = document.createElement('div');
    pop.className = 'db-suggest';
    pop.id = 'db-sql-suggest';
    pop.setAttribute('role', 'listbox');
    pop.setAttribute('aria-label', 'SQL 补全');
    pop.hidden = true;
    pop.innerHTML = '<div class="db-suggest-list" role="presentation"></div><div class="db-suggest-hint"></div>';
    // 模态 dialog 会进入顶层（top layer），挂在 body 下的浮层无法显示在其上方；
    // 因此挂进最近的 dialog（没有则挂 body），fixed 定位仍按视口计算。
    const host = textarea.closest('dialog') || document.body;
    host.append(pop);
    const list = pop.querySelector('.db-suggest-list');
    const hint = pop.querySelector('.db-suggest-hint');
    textarea.setAttribute('aria-autocomplete', 'list');
    textarea.setAttribute('aria-controls', pop.id);

    let items = [];
    let active = 0;
    let refreshTries = 0;
    let anchor = 0;
    const visible = () => !pop.hidden && items.length > 0;

    function setExpanded() {
      textarea.setAttribute('aria-expanded', String(visible()));
    }
    function hide() {
      if (pop.hidden) return;
      pop.hidden = true;
      items = [];
      active = 0;
      setExpanded();
    }
    function locate() {
      const point = caretPoint(textarea, anchor);
      const box = textarea.getBoundingClientRect();
      const width = Math.min(620, Math.max(340, box.width));
      const height = pop.offsetHeight || 200;
      const spaceBelow = window.innerHeight - box.bottom;
      const up = spaceBelow < height + 12 && box.top > height + 12;
      pop.dataset.placement = up ? 'up' : 'down';
      const left = Math.max(8, Math.min(box.left + point.left - 6, window.innerWidth - width - 8));
      const top = up ? box.top - height - 6 : box.bottom + 6;
      pop.style.width = width + 'px';
      pop.style.left = left + 'px';
      pop.style.top = Math.max(8, top) + 'px';
    }
    function paint() {
      list.replaceChildren();
      items.forEach((word, index) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'db-suggest-item';
        row.setAttribute('role', 'option');
        row.tabIndex = -1;
        row.dataset.index = String(index);
        row.dataset.active = String(index === active);
        row.setAttribute('aria-selected', String(index === active));
        const tag = document.createElement('span');
        tag.className = 'db-suggest-tag';
        tag.textContent = word.replace(/\s+/g, ' ').replace(/[`"[]/g, '').split(' ')[0].toUpperCase();
        const text = document.createElement('span');
        text.className = 'db-suggest-text';
        text.textContent = word;
        row.append(tag, text);
        // 按下时阻止默认行为，避免 textarea 先失焦导致候选在 click 前被收起。
        row.addEventListener('mousedown', (event) => event.preventDefault());
        row.addEventListener('click', () => accept(index));
        list.append(row);
      });
      hint.textContent = TYPING;
      pop.hidden = false;
      setExpanded();
      locate();
    }
    function highlight() {
      for (const row of list.children) {
        const on = Number(row.dataset.index) === active;
        row.dataset.active = String(on);
        row.setAttribute('aria-selected', String(on));
        if (on) row.scrollIntoView({ block: 'nearest' });
      }
    }
    // 求当前光标处的候选：有前缀按前缀过滤；没有前缀时只给命令头（如 SELECT），
    // FROM/WHERE 之后不主动展开整张表或全部字段，避免遮挡界面。
    function suggestions() {
      const before = textarea.value.slice(0, textarea.selectionStart);
      const word = C.currentWord(before);
      const rest = before.slice(0, before.length - word.length);
      // 每次按键都读一次最新上下文：表名目录/字段可能在前一个按键后才到达。
      const data = getContext() || {};
      const words = C.contextWords(rest, data.columns || [], data.tables || []);
      // 表名位置：FROM/JOIN/UPDATE/INTO 之后（含刚敲完空格的空表名）。
      const tablePosition = /(?:^|\s)(?:from|join|into|update|table)\s+[\w."`]*$/i.test(rest);
      // 表名位置以表为先（pr → products），其余位置关键字优先（sel → SELECT）。
      // 表名位置只给表与片段；其它位置关键字与表名合并（输入 d 时 DESC 与 desc_tb 一起出现）。
      // 合并方式与传入顺序无关：<(前缀 1) 匹配上下文��、<(前缀 2) 匹配普通关键字>，
      // 因此表名位置先给表与片段，其余位置关键字与表名一起出现（sel→SELECT，pr→products）。
      // 表名位置只给表与片段（目录未就绪时宁可少给，也不给 PRIMARY KEY 这类无关词）。
      // 表名位置：以输入前缀开头的表名优先，其次是指令片段；目录很大时也只给少量。
      let list = tablePosition ? C.tableSuggest(words, word, MAX_WORDS) : C.suggest(C.KEYWORDS, words, word, MAX_WORDS, 1);
      // 已输入的词不再作为候选提示（光标停在 FROM 后时不该再提示一个 FROM）。
      list = list.filter((item) => item.trim().toLowerCase() !== word.toLowerCase());
      if (!word && C.atStatementStart(rest) && C.atWordStart(before)) list = C.wordsUnique(['SELECT ', ...words]).slice(0, MAX_WORDS);
      return { list, needRefresh: Boolean(data.notReady) };
    }
    function compute() {
      // 只读编辑器状态，不把选区传给 suggestions：焦点短暂离开时（点击、切换窗口）
      // selectionStart/End 可能为 0，那会把“没输入前缀”误当成“选中了文本”。
      if (!textarea.isConnected || textarea.disabled) { hide(); return; }
      // 选中一段非空文本时不弹候选（只会替换掉选区）；空选区或 0:0 都按插入点处理。
      if (textarea.selectionEnd > textarea.selectionStart) { hide(); return; }
      const before = textarea.value.slice(0, textarea.selectionStart);
      const { list, needRefresh } = suggestions();
      items = list;
      anchor = textarea.selectionStart - C.currentWord(before).length;
      active = 0;
      if (items.length) paint(); else hide();
      // 表名/字段晚于按键到达时，用同一段文字重算一次，候选随数据就绪补齐（最多重试几次）。
      if (needRefresh && refreshTries < 3) { refreshTries += 1; setTimeout(() => { if (visible() && document.activeElement === textarea) compute(); }, 300); }
    }
    function move(delta) {
      active = (active + delta + items.length) % items.length;
      highlight();
    }
    function accept(index) {
      const word = items[index === undefined ? active : index];
      if (word === undefined) return;
      const start = anchor;
      const end = textarea.selectionStart;
      // 已输入开头引号时，「na」替换为「"name"」而不是「""name""」。
      const typed = textarea.value.slice(start, end);
      const leading = /^["`[]/.test(typed) && word.startsWith(typed[0]) ? typed[0] : '';
      textarea.setRangeText(leading ? word.slice(typed.length) : word, start, end, 'end');
      hide();
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      textarea.focus();
    }
    // 不能直接把 compute 当监听器：事件对象会被当成 index 参数（undefined）。
    textarea.addEventListener('input', () => compute());
    // 表名/字段到达时只在用户仍停留在编辑器、且补全列表仍打开时重算。
    textarea.addEventListener('sql-context', () => { if (visible() && document.activeElement === textarea) compute(); });
    textarea.addEventListener('click', hide);
    textarea.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== textarea) hide(); }, 120));
    textarea.addEventListener('keydown', (event) => {
      // 补全打开时 Esc 只关补全，不能让浏览器把这次 Esc 继续当成“关闭对话框”。
      if (event.key === 'Escape' && visible()) { event.preventDefault(); event.stopPropagation(); hide(); return; }
      if (!visible() || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
      else if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); accept(); }
    });
    window.addEventListener('resize', () => { if (visible()) locate(); });
    return { compute, hide, visible, items: () => items, accept, move, pop };
  }
  window.SqlCompletionUI = { attach, caretPoint };
})();
