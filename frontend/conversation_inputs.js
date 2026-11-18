/* Turn supplements and user-owned quote snapshots; no synthetic task completion. */
window.ConversationInputsUI = (() => {
  let references = [], target = null, revision = 0, selected = null;
  const tray = document.createElement("div"); tray.className = "reference-tray hidden"; tray.id = "reference-tray";
  document.getElementById("input").before(tray);
  // 发送方式不再由用户选：回合进行中默认「补充当前任务」（插入到当前回合，
  // 下一次模型请求前纳入），回合结束后就是「下一项任务」。mode 保留为不渲染的
  // 隐形占位，只为既有读写路径（save/restore/delivery()/outgoingItem）不改签名。
  const mode = document.createElement("select"); mode.id = "delivery-mode"; mode.className = "chip hidden";
  mode.setAttribute("aria-label","发送方式");
  for (const [value,label] of [["queue","下一项任务"],["steer","补充当前任务"]]) {
    const option=document.createElement("option");option.value=value;option.textContent=label;mode.append(option);
  }
  const quote = document.createElement("button");quote.type="button";quote.className="quote-selection-action hidden";
  quote.textContent="引用提问";document.body.append(quote);
  const dialog=document.createElement("dialog");dialog.className="reference-view-dialog";
  const title=document.createElement("h3");title.textContent="引用片段";
  const snapshot=document.createElement("blockquote");
  const close=document.createElement("button");close.textContent="关闭";close.onclick=()=>dialog.close();
  const jump=document.createElement("button");jump.textContent="定位原文";
  dialog.append(title,snapshot,jump,close);document.body.append(dialog);
  const draftKey=sid=>`conversation-input_${who}_${sid || "new"}`;
  // 回合是否仍在跑只由同步传入的 running 决定（不再看 mode.value）。
  // 回合进行中：补充当前任务；回合结束：下一项任务。
  let running=false;
  function wanted() { return running && activeTurnMid ? "steer" : "queue"; }
  function applyWanted() {
    const value=wanted();
    mode.value=value;
    // 目标回合：进行中钉住当前回合；结束/切换后原回合已不可补，清掉
    target=value==="steer" ? activeTurnMid : null;
  }
  function save(sid) {
    const value={references,delivery:mode.value,target};
    if (references.length || mode.value==="steer") localStorage.setItem(draftKey(sid),JSON.stringify(value));
    else localStorage.removeItem(draftKey(sid));
  }
  function sync(isRunning=!!streaming) { running=!!isRunning; applyWanted(); mode.classList.add("hidden"); }
  function drawTray() {
    tray.replaceChildren();tray.classList.toggle("hidden",!references.length);
    references.forEach((reference,index)=>{
      const pill=document.createElement("span");pill.className="reference-pill";
      const preview=document.createElement("button");preview.type="button";
      preview.textContent="引用 · "+reference.text.slice(0,32);preview.title=reference.text;
      preview.onclick=()=>showReference(reference);
      const remove=document.createElement("button");remove.type="button";remove.textContent="×";remove.setAttribute("aria-label","移除引用");
      remove.onclick=()=>{references.splice(index,1);drawTray();save(currentSession);};
      pill.append(preview,remove);tray.append(pill);
    });
  }
  function restore(sid) {
    revision++;quote.classList.add("hidden");selected=null;if(dialog.open)dialog.close();
    let stored={};try {stored=JSON.parse(localStorage.getItem(draftKey(sid))) || {};} catch {}
    references=Array.isArray(stored.references) ? stored.references.filter(r=>typeof r.mid==="string" && typeof r.text==="string").slice(0,5) : [];
    // 草稿里的 delivery 只作留痕（曾经手动选过），不再回填：发送方式此刻由
    // 回合状态决定，见 applyWanted()。
    drawTray();sync();
  }
  async function showReference(reference) {
    const sid=currentSession,view=sessionViewVersion;
    snapshot.textContent=reference.text;dialog.showModal();
    jump.onclick=async()=>{
      try {
        let node=document.querySelector(`#chat [data-mid="${CSS.escape(reference.mid)}"]`);
        if (!node) {
          const data=await api(`/api/sessions/${encodeURIComponent(sid)}/messages?mid=${encodeURIComponent(reference.mid)}`);
          const message=data.messages.find(m=>m.mid===reference.mid);
          if (!message) throw new Error("原消息已不存在，引用快照仍可查看");
          if(sid!==currentSession || view!==sessionViewVersion)return;
          await loadWindowAround(message.ord);
          node=document.querySelector(`#chat [data-mid="${CSS.escape(reference.mid)}"]`);
        }
        if(sid!==currentSession || view!==sessionViewVersion)return;
        if(!node)throw new Error("原文暂未加载，引用快照仍可查看");
        dialog.close();stickBottom=false;node.scrollIntoView({block:"center"});node.classList.add("quote-source-highlight");
        setTimeout(()=>node.classList.remove("quote-source-highlight"),1500);
      } catch(error) { if(sid===currentSession)toast(error.message); }
    };
  }
  function referenceButton(reference) {
    const button=document.createElement("button");button.type="button";button.className="ub-reference";
    button.textContent="引用"+(reference.source_role==="assistant" ? "回复" : "需求");
    button.title=reference.text;button.onclick=()=>showReference(reference);return button;
  }
  function detectSelection(event) {
    if(event.target===quote)return;
    const selection=getSelection();quote.classList.add("hidden");selected=null;
    if(!selection || selection.isCollapsed || !selection.rangeCount || !currentSession)return;
    const element=node=>node?.nodeType===1 ? node : node?.parentElement;
    const start=element(selection.anchorNode)?.closest("#chat .bubble");
    const end=element(selection.focusNode)?.closest("#chat .bubble");
    if(!start || start!==end)return;
    const source=start.dataset.mid ? start : start.closest("[data-mid]");
    if(!source || !["user","assistant"].includes(source.dataset.role))return;
    if(start.classList.contains("assistant") && !start.dataset.persisted)return;
    const text=selection.toString().trim();if(!text)return;
    selected={mid:source.dataset.mid,text};
    const rect=selection.getRangeAt(0).getBoundingClientRect();
    quote.style.left=Math.max(8,Math.min(rect.right-90,innerWidth-100))+"px";
    quote.style.top=Math.max(8,Math.min(rect.bottom+6,innerHeight-44))+"px";
    quote.classList.remove("hidden");
  }
  document.addEventListener("mouseup",detectSelection);
  document.addEventListener("selectionchange",detectSelection);
  quote.onmousedown=event=>event.preventDefault();
  quote.onclick=()=>{
    if(!selected)return;
    const next=[...references,selected];
    if(next.length>5 || new TextEncoder().encode(JSON.stringify(next)).length>7000) {toast("引用最多5条，请缩小到约7KB以内，为来源信息预留空间");return;}
    references=next;drawTray();save(currentSession);quote.classList.add("hidden");inputEl.focus();
  };
  function decorate(node,message) {
    const user=node.matches?.('[data-role="user"]') ? node : node.querySelector?.('[data-role="user"]');
    if(!user || !message.supplements?.length)return;
    renderSupplements(user,message.supplements);
  }
  function renderSupplements(user,items) {
    const group=user.closest(".msg-group") || user;
    // user 气泡本身就是 .msg-group（还没包动作条）：组上的 is-user 决定对齐，
    // 补上后时间线里它和常规用户消息一样靠右。
    group.classList.add("is-user");
    let list=group.querySelector(".turn-input-list");
    if(!list){list=document.createElement("div");list.className="turn-input-list";group.append(list);}
    const opened=new Set([...list.querySelectorAll("details[open]")].map(node=>node.dataset.supplementId));
    list.replaceChildren();
    for(const item of items){
      const detail=document.createElement("details");detail.dataset.supplementId=item.id;detail.open=opened.has(item.id);
      detail.classList.add("supplement-item",`st-${item.state}`);
      const summary=document.createElement("summary");
      // 折叠态只有一行：▸ 补充 [状态胶囊] 正文预览（CSS 单行省略，不按字数截，
      // 免得宽屏浪费空间/窄屏反而溢出）。点开才出全文与引用/复制按钮。
      const tag=document.createElement("span");tag.className="supp-tag";tag.textContent="补充";
      const state=document.createElement("span");state.className="supp-state";
      state.textContent={accepted:"等待纳入",applied:"已纳入",expired:"未纳入"}[item.state] || item.state;
      state.title={accepted:"已收到，将在下一次模型请求前纳入",applied:"已纳入本次任务的上下文",
        expired:"任务结束或中断，本条补充未被采用"}[item.state] || "";
      const preview=document.createElement("span");preview.className="supp-text";
      preview.textContent=item.text.replace(/\s+/g," ").trim();
      summary.append(tag,state,preview);
      const text=document.createElement("p");text.textContent=item.text;
      detail.append(summary,text);
      for(const reference of item.references || [])detail.append(referenceButton(reference));
      const copy=document.createElement("button");copy.type="button";copy.textContent="复制补充";
      copy.onclick=async()=>toast(await copyText(item.text) ? "已复制补充内容" : "复制失败");
      detail.append(copy);list.append(detail);
    }
  }
  async function refresh() {
    const sid=currentSession,view=sessionViewVersion,token=authToken,seq=++revision;
    if(!sid || !token)return;
    try {
      const query=activeTurnMid ? "?user_mid="+encodeURIComponent(activeTurnMid) : "";
      const data=await api(`/api/sessions/${encodeURIComponent(sid)}/inputs`+query);
      if(seq!==revision || sid!==currentSession || view!==sessionViewVersion || token!==authToken)return;
      const grouped=new Map();for(const item of data.items){if(!grouped.has(item.user_mid))grouped.set(item.user_mid,[]);grouped.get(item.user_mid).push(item);}
      for(const [mid,items] of grouped){const node=railFindNode(mid);if(node)renderSupplements(node,items);}
    } catch { /* A failed send preserves its original outbox identity and reports separately. */ }
  }
  setInterval(()=>{if(!document.hidden && streaming)void refresh();},5000);
  return {save,restore,sync,refresh,decorate,referenceButton,
    selection:()=>references.map(r=>({...r})),
    // 发送时按【此刻】的回合状态现算一次，不依赖元素状态是否refresh过
    delivery:()=>{applyWanted();return {delivery:mode.value,targetMid:target};},
    // 清空只清引用；发送方式由回合状态即时决定（applyWanted），不在这里写死
    clear(){references=[];applyWanted();drawTray();sync();},
    fromItem(item){references=item.references || [];applyWanted();drawTray();sync();}
  };
})();
