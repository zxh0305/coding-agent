/* Checks survive SSE buffer eviction through their session-owned history API. */
window.TurnChecksUI = (() => {
  const turns = new Map();
  function row(mid) {
    if (!turns.has(mid)) turns.set(mid, {mid, sid:currentSession, checks:new Map(), node:null, assessment:null, revision:0});
    return turns.get(mid);
  }
  function draw(entry) {
    const items = [...entry.checks.values()];
    const latest = Math.max(0, ...items.map(item => item.attempt || 0));
    const results = items.filter(item => (item.attempt || 0) === latest);
    const labels = {passed:"声明范围的检查通过",failed:"检查仍有失败",blocked:"检查受阻",
                    insufficient:"验证不足",cancelled:"检查已停止",interrupted:"检查被中断"};
    const reason = entry.assessment?.reason || labels[entry.assessment?.assessment] || "检查详情正在加载";
    const card = blocksRenderer.renderBlock({kind:"checks", verification:{results, reason}});
    const title = card.querySelector("summary");
    if (!results.length && entry.assessment) title.textContent = labels[entry.assessment.assessment] || "自动检查";
    const previous = items.filter(item => (item.attempt || 0) !== latest);
    if (previous.length) {
      const old = blocksRenderer.renderBlock({kind:"checks", verification:{results:previous}});
      old.querySelector("summary").textContent = "查看前次检查";
      card.appendChild(old);
    }
    const opened = entry.node?.open || false;
    card.open = opened;
    card.dataset.checkMid = entry.mid;
    if (entry.node?.isConnected) entry.node.replaceWith(card);
    entry.node = card;
    card.addEventListener("toggle", async () => {
      if (!card.open || entry.loaded || entry.loading) return;
      entry.loading = true;
      const view = sessionViewVersion, revision = entry.revision;
      try {
        const data = await api(`/api/sessions/${encodeURIComponent(entry.sid)}/checks?user_mid=${encodeURIComponent(entry.mid)}`);
        if (view !== sessionViewVersion || currentSession !== entry.sid || turns.get(entry.mid) !== entry) return;
        for (const item of data.checks) {
          if (revision === entry.revision || !entry.checks.has(item.check_id)) {
            entry.checks.set(item.check_id, {...item,session_id:entry.sid});
          }
        }
        if (revision === entry.revision) entry.assessment = data.assessment;
        entry.loaded = revision === entry.revision;
        draw(entry);
      } catch(e) { if (view === sessionViewVersion) toast("读取检查失败：" + e.message); }
      finally { entry.loading = false; }
    });
    return card;
  }
  function history(message) {
    const entry = row(message.mid);
    entry.assessment = message.assessment;
    const card = draw(entry);
    return card;
  }
  return {
    reset() { turns.clear(); },
    has(mid) { return turns.has(mid); },
    history,
    phase(event) {
      if (!event.user_mid) return;
      if (event.assessment === "not_applicable" && event.phase === "finished") return;
      if (!turns.has(event.user_mid) && (!event.assessment || event.assessment === "not_applicable")) return;
      const entry = row(event.user_mid);
      entry.revision++;
      entry.assessment = event;
      const existed = entry.node?.isConnected;
      const card = draw(entry);
      if (!existed) chatEl.appendChild(card);
    },
    update(event) {
      const entry = row(event.user_mid);
      entry.revision++;
      entry.checks.set(event.check_id, {...event, session_id:currentSession});
      const existed = entry.node?.isConnected;
      const card = draw(entry);
      if (!existed) chatEl.appendChild(card);
    },
    async loadOutput(check, pre, button) {
      const sid = check.session_id || currentSession, view = sessionViewVersion;
      button.disabled = true;
      try {
        const offset = button.dataset.offset || "1";
        const data = await api(`/api/sessions/${encodeURIComponent(sid)}/checks/${encodeURIComponent(check.check_id)}/output?offset=${offset}&limit=200`);
        if (view !== sessionViewVersion || currentSession !== sid || !pre.isConnected) return;
        if (offset === "1") pre.textContent = check.cmd + "\n" + data.lines.join("\n");
        else pre.textContent += "\n" + data.lines.join("\n");
        const next = data.shown[1] + 1;
        button.dataset.offset = String(next);
        button.textContent = next <= data.total_lines ? `继续读取（共 ${data.total_lines} 行）` : "输出已读完";
        button.disabled = next > data.total_lines;
      } catch(e) { if (view === sessionViewVersion) toast("读取输出失败：" + e.message); }
      finally { if (button.textContent !== "输出已读完") button.disabled = false; }
    },
  };
})();
