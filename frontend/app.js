/* 前端逻辑：任务列表、常驻事件流对话、执行过程时间线、模型/工作区/上下文工具栏。
   原生 JS，无框架、无构建步骤。 */

const $ = (id) => document.getElementById(id);
const chatEl = $("chat");
const inputEl = $("input");

let currentSession = null;   // 当前任务（会话）id；null = 将开新任务
let streaming = false;       // 正在生成回答：此时发送按钮变身停止按钮（由 turn_start/turn_end 事件驱动）
let contextWindow = 262144;  // 上下文容量显示上限（/api/config 提供）
let usageNow = null;         // 最近一次 usage 事件（含上下文构成）

// ---------- 登录状态 ----------
// token 放 localStorage：刷新不掉线；后端把它存进 agent_data.db，服务重启也不掉线。
let authToken = localStorage.getItem("auth_token") || "";
let who = localStorage.getItem("auth_username") || "";
let loginMode = "login";    // "login" | "register"

function authHeaders() {
  return authToken ? { Authorization: `Bearer ${authToken}` } : {};
}

function setWho(name) {
  // 左下角头像取用户名首字符；设置面板同步
  who = name || "";
  const initial = (who || "牛").slice(0, 1);
  $("user-avatar").textContent = initial;
  $("user-name").textContent = who || "—";
  $("pop-avatar").textContent = initial;
  $("pop-name").textContent = who || "—";
}

function showLogin() {
  authToken = "";
  who = "";
  currentSession = null;      // 下一个登录者不能沿用上一个用户的任务 id
  chatEl.innerHTML = "";
  pendingQueue = [];          // 排队消息也作废（它们属于上一个用户的任务）
  closeEvents();              // 事件流属于上一个登录者，立即断开
  resetStreamState();         // 流式状态同样属于上一个用户/任务
  localStorage.removeItem("auth_token");
  localStorage.removeItem("auth_username");
  $("login-error").textContent = "";
  $("layout").classList.add("hidden");     // 隐藏对话页
  $("login-page").classList.remove("hidden");  // 显示独立登录页
  $("login-user").focus();
}

function setLoginMode(mode) {
  loginMode = mode;
  $("login-mode").textContent = mode === "login" ? "没有账号？点此注册" : "已有账号？点此登录";
  $("login-submit").textContent = mode === "login" ? "登录" : "注册并进入";
  $("login-error").textContent = "";
}

async function submitLogin() {
  const username = $("login-user").value.trim();
  const password = $("login-pass").value;
  const errEl = $("login-error");
  errEl.textContent = "";
  $("login-submit").disabled = true;
  try {
    const data = await api(`/api/auth/${loginMode}`, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    authToken = data.token;
    who = data.username;
    localStorage.setItem("auth_token", authToken);
    localStorage.setItem("auth_username", who);
    $("login-page").classList.add("hidden");
    $("login-pass").value = "";
    boot();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    $("login-submit").disabled = false;
  }
}

// ---------- 后端 API 封装 ----------
async function api(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
      },
      ...options,
    });
  } catch (e) {
    // fetch 抛错基本只有一种情况：后端没启动或地址不通
    throw new Error("无法连接后端服务，请先运行 python3 backend/app.py 再刷新页面");
  }
  // 401 = 未登录/掉线：弹登录层（auth 接口自身除外，那里 401 是密码错误）
  if (resp.status === 401 && !path.startsWith("/api/auth/")) {
    showLogin();
    throw new Error("未登录或登录已失效");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// ---------- 小工具 ----------
function buildBubble(className, text) {
  const div = document.createElement("div");
  div.className = `bubble ${className}`;
  div.textContent = text;
  return div;
}

function bubble(className, text) {
  const div = buildBubble(className, text);
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

function fmtBytes(n) {
  if (n == null) return "—";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + "KB";
  return n + "B";
}

function summarize(s, n) {
  s = (s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function prettyJson(s) {
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; }
}

function fmtWan(n) {
  if (n == null) return "—";
  if (n >= 10000) return (n / 10000).toFixed(n >= 100000 ? 0 : 1) + "万";
  return String(n);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const pad = (x) => String(x).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const days = Math.floor((now - d) / 86400000);
  if (days >= 0 && days < 7) return `${days}天前`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 2500);
}

// ---------- 任务（会话）列表 ----------
let confirmingDelete = null;  // 正处于"确认删除"状态的任务 id（二次确认，防误触）
let sessionsCache = [];       // 最近一次拉取的任务列表，删除的乐观更新直接改它

async function loadSessions() {
  try {
    const list = await api("/api/sessions");
    sessionsCache = Array.isArray(list) ? list : [];
    renderSessions(sessionsCache);
  } catch (e) { /* 启动时后端未就绪不打扰 */ }
}

function removeSessionLocal(id) {
  // 乐观更新：点击"删除"瞬间先在本地移除该行（请求后台进行），失败再回滚刷新。
  // 原先要等 DELETE + 列表刷新两个往返都回来 UI 才动，页面忙时会被感知成"点了没反应"。
  sessionsCache = sessionsCache.filter(s => s.id !== id);
  renderSessions(sessionsCache);
}

function renderSessions(list) {
  const ul = $("task-list");
  ul.innerHTML = "";
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "task empty";
    li.textContent = "（还没有任务，发一条消息即创建）";
    ul.appendChild(li);
    return;
  }
  for (const s of list) {
    const li = document.createElement("li");
    li.className = "task" + (s.id === currentSession ? " active" : "");

    // 二次确认状态：这一行变成"确认删除？[删除][取消]"，不做弹窗
    if (s.id === confirmingDelete) {
      li.classList.add("confirming");
      const q = document.createElement("div");
      q.className = "t-question";
      q.textContent = "确认删除？";
      const yes = document.createElement("button");
      yes.className = "t-yes";
      yes.textContent = "删除";
      yes.addEventListener("click", (e) => {
        e.stopPropagation();
        confirmingDelete = null;
        doDeleteSession(s.id);
      });
      const no = document.createElement("button");
      no.className = "t-no";
      no.textContent = "取消";
      no.addEventListener("click", (e) => {
        e.stopPropagation();
        confirmingDelete = null;
        renderSessions(list);
      });
      li.append(q, yes, no);
      ul.appendChild(li);
      continue;
    }

    const title = document.createElement("div");
    title.className = "t-title";
    title.textContent = s.title || "新任务";
    const time = document.createElement("span");
    time.className = "t-time";
    time.textContent = fmtTime(s.updated);
    const del = document.createElement("button");
    del.className = "t-del";
    del.textContent = "🗑";
    del.title = "删除任务";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      confirmingDelete = s.id;   // 第一次点：只进入确认状态，不真删
      renderSessions(list);
    });
    li.append(title, time, del);
    li.title = s.title || "";
    li.addEventListener("click", () => {
      confirmingDelete = null;
      switchSession(s.id);
    });
    ul.appendChild(li);
  }
}

async function doDeleteSession(id) {
  removeSessionLocal(id);  // 先让行消失（即时反馈），请求在后台进行
  try {
    await api(`/api/sessions?session_id=${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (e) {
    toast("删除失败：" + e.message);
    loadSessions();  // 回滚：以服务端列表为准
    return;
  }
  if (currentSession === id) {
    // 删的是当前打开的任务：清空对话区，回到待新建状态；事件流随任务一起
    // 消失（其他标签页的连接由 session_deleted 事件收摊）
    currentSession = null;
    resetStreamState();
    chatEl.innerHTML = "";
    welcome();
    refreshCtx();
    closeEvents();
    historyMids = new Set();
  }
  loadSessions();  // 与服务端对齐一次（时间戳/排序），不阻塞交互
}

async function newTask() {
  currentSession = null;
  confirmingDelete = null;
  resetStreamState();  // 旧任务的事件流已断，流式状态必须随之复位
  closeEvents();      // 旧任务的事件流断开：新任务未建，第一条消息发出后再连
  chatEl.innerHTML = "";
  welcome();
  usageNow = null;
  updateCtxChip();
  loadWorkspace();  // 回到"新任务"态：工具栏显示用户默认工作区
  await loadSessions();  // 重新拉取列表：旧任务仍显示，只是没有选中项；首条消息后新任务才出现
}

// ---------- 历史消息回放（分页加载 + 外置归档） ----------
let histOldestOrd = null;  // 已加载最旧一条消息的 ord（向上翻页游标）
let histHasMore = false;   // 其上是否还有更早的消息

async function switchSession(id) {
  if (id === currentSession) return;
  currentSession = id;
  resetStreamState();  // 旧会话的事件流已断，流式状态必须随之复位
  chatEl.innerHTML = "";
  welcome();
  histOldestOrd = null;
  histHasMore = false;
  historyMids = new Set();        // 补发去重基准随任务重建
  await loadHistoryPage();        // 时间线先行：补发定性（finishBoot）要拿它比对
  openEvents(id);                 // 再接事件流：断线/刷新期间的回合靠 since 补发接上
  await loadSessions();
  await refreshCtx();
  loadWorkspace();  // 每个任务有自己的工作区：切换后工具栏跟着换
  dispatchNextQueued();  // 切回有排队消息的任务时，接着把排队的发出去
}

async function loadHistoryPage() {
  if (!currentSession) return;
  try {
    // 默认最近 100 条；向上翻页带 before_ord（已加载最旧一条的 ord）
    const qs = new URLSearchParams({ limit: "100" });
    if (histOldestOrd != null) qs.set("before_ord", histOldestOrd);
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}/messages?` + qs);
    histHasMore = !!data.has_more;
    if (!data.messages.length) { updateLoadOlder(); return; }
    histOldestOrd = data.messages[0].ord;
    const frag = document.createDocumentFragment();
    for (const m of data.messages) {
      if (m.mid) historyMids.add(m.mid);  // 事件流补发去重的比对基准
      frag.appendChild(historyNode(m));
    }
    const btn = $("load-older");
    if (btn) {
      // 向上翻页：更早的消息插在"加载更早"按钮之后、现有历史之前；
      // 补偿滚动位置，用户视线的消息不跳动
      const prevHeight = chatEl.scrollHeight, prevTop = chatEl.scrollTop;
      btn.after(frag);
      chatEl.scrollTop = prevTop + (chatEl.scrollHeight - prevHeight);
    } else {
      chatEl.appendChild(frag);
      chatEl.scrollTop = chatEl.scrollHeight;
    }
    updateLoadOlder();
  } catch (e) { /* 历史拉取失败不阻塞 */ }
}

function updateLoadOlder() {
  const old = $("load-older");
  if (!histHasMore) { if (old) old.remove(); return; }
  if (old) return;
  const btn = document.createElement("button");
  btn.id = "load-older";
  btn.className = "load-older";
  btn.textContent = "⬆ 加载更早的消息";
  btn.addEventListener("click", loadHistoryPage);
  // 插在欢迎语（第一个子节点）之后、历史消息之前
  chatEl.insertBefore(btn, chatEl.children[1] || null);
}

// 单条历史消息 → DOM 节点（与实时对话一致的渲染规则）
function historyNode(m) {
  // 外置归档消息：库行内只有 head 预览（超大正文存 artifacts 文件），
  // 展示"内容过大已归档"标记，点开按需拉取全文
  if (m.artifact) return artifactCard(m);
  if (m.role === "compact") return compactCard(m.content);
  if (m.role === "user" && Array.isArray(m.content)) {
    const text = m.content
      .filter(p => p.type === "text")
      .map(p => (p.text || "").length > 600 ? p.text.slice(0, 600) + "…[附件内容已折叠]" : p.text)
      .join("\n");
    const imgs = m.content
      .filter(p => p.type === "image_url")
      .map(p => ({ kind: "image", name: "", preview: (p.image_url || {}).url || "" }));
    return buildUserBubble(text, imgs);
  }
  // 用 fragment 直接把气泡/meta 挂进 #chat：外面包一层普通 div 会让
  // .bubble.user 的 align-self 失效（父级不是 flex），用户消息就会挤到左侧
  const frag = document.createDocumentFragment();
  frag.appendChild(buildBubble(m.role === "user" ? "user" : "assistant", m.content || ""));
  // 历史消息也带回当时的耗时/token 统计（message_usage 表随消息附带）
  if (m.role === "assistant" && m.stats) {
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = metaText(m.stats.elapsed_s, m.stats.usage);
    frag.appendChild(meta);
  }
  return frag;
}

// 外置归档消息卡片：head 预览 + 归档标记；点开懒加载全文（1MB 级内容
// 不随时间线整页带回，用户要看时才走 artifact 接口取）。
// 带图片的用户消息（base64 多模态 content 必然超 64KB 行内上限）点开后
// 还原成正常的用户气泡——图片渲染出来，而不是把 base64 JSON 摆在 <pre> 里。
function artifactCard(m) {
  const d = document.createElement("details");
  d.className = "bubble assistant artifact";
  const s = document.createElement("summary");
  s.textContent = `📦 内容过大已归档（${fmtBytes(m.bytes)}）· 点开加载全文`;
  const box = document.createElement("div");
  const pre = document.createElement("pre");
  pre.className = "artifact-preview";
  pre.textContent = m.head || "（无预览）";
  box.appendChild(pre);
  d.append(s, box);
  d.addEventListener("toggle", async () => {
    if (!d.open || d.dataset.loaded) return;
    d.dataset.loaded = "1";
    try {
      const r = await api(`/api/sessions/${encodeURIComponent(currentSession)}` +
        `/artifact?path=${encodeURIComponent(m.path)}`);
      const msg = r.message;
      const parts = Array.isArray(msg.content) ? msg.content : null;
      if (msg.role === "user" && parts) {
        const text = parts.filter(p => p.type === "text")
          .map(p => p.text || "").join("\n");
        const imgs = parts.filter(p => p.type === "image_url")
          .map(p => ({ kind: "image", name: "", preview: (p.image_url || {}).url || "" }));
        box.innerHTML = "";
        box.appendChild(buildUserBubble(text, imgs));  // data URI 直接进 <img src>
      } else {
        pre.textContent = prettyJson(JSON.stringify(msg));
      }
    } catch (e) {
      pre.textContent = "全文读取失败：" + e.message;
    }
  });
  return d;
}

function welcome() {
  bubble("assistant", "你好！我是 Agent 助手。可以问我：「北京今天天气怎么样」「37*89+100 等于多少」，或让我在工作区里写代码、修 bug。");
}

// ---------- 附件（图片 / 文本文件）----------
// 图片以 base64 作为视觉输入发给模型；文本文件解码后注入上下文。
// 都只存在数据库的消息里，不另外落盘。
let attachments = [];  // {kind: "image"|"text", name, mime, data(base64), preview}

const MAX_ATTACH = 6;

function renderAttachTray() {
  const tray = $("attach-tray");
  if (!attachments.length) {
    tray.classList.add("hidden");
    tray.innerHTML = "";
    return;
  }
  tray.classList.remove("hidden");
  tray.innerHTML = "";
  attachments.forEach((a, i) => {
    const card = document.createElement("div");
    card.className = "att-card";
    card.title = a.name;
    if (a.kind === "image" && a.preview) {
      const img = document.createElement("img");
      img.src = a.preview;
      img.alt = a.name;
      card.appendChild(img);
    } else {
      const icon = document.createElement("div");
      icon.className = "att-file";
      icon.textContent = "📄 " + summarize(a.name, 14);
      card.appendChild(icon);
    }
    const x = document.createElement("button");
    x.className = "att-del";
    x.textContent = "✕";
    x.title = "移除";
    x.addEventListener("click", () => {
      attachments.splice(i, 1);
      renderAttachTray();
    });
    card.appendChild(x);
    tray.appendChild(card);
  });
}

function addFileToAttachments(file) {
  if (!file) return;
  if (attachments.length >= MAX_ATTACH) { toast(`一次最多 ${MAX_ATTACH} 个附件`); return; }
  const isImage = file.type.startsWith("image/");
  if (isImage && file.size > 4 * 1024 * 1024) { toast(`图片超过 4MB`); return; }
  if (!isImage && file.size > 300 * 1024) { toast(`文件超过 300KB（文本附件限制）`); return; }
  if (isImage && !activeModelVision) {
    toast("当前模型未标注视觉能力，发送后将由 analyze_image 工具代为识别");
  }
  // FileReader 是异步的：读取期间用户可能删除其他附件（数组前移/缩短），
  // 按读取开始时的下标回写会错位。这里先占一个真实位置，回写前核对
  // （token 不匹配 = 列表变过，重新找位置；找不到说明该附件已被移除，丢弃）。
  const entry = isImage
    ? { kind: "image", name: file.name || `clipboard.${(file.type.split("/")[1] || "bin").replace("+xml", "")}`,
        mime: file.type, data: "", preview: "" }
    : { kind: "text", name: file.name || "clipboard.txt", mime: "text/plain", data: "", preview: "" };
  attachments.push(entry);  // 占位：先出现在托盘里（空 data），读完后回填
  renderAttachTray();
  const reader = new FileReader();
  reader.onload = () => {
    const dataUrl = String(reader.result);
    const data = dataUrl.slice(dataUrl.indexOf(",") + 1);  // 去掉 data:...;base64, 前缀
    const i = attachments.indexOf(entry);  // 按对象身份找位置，不按下标猜
    if (i < 0) return;  // 读取期间被用户移除：丢弃，不写回
    entry.data = data;
    entry.preview = isImage ? dataUrl : "";
    renderAttachTray();
  };
  reader.readAsDataURL(file);
}

function onFilesChosen() {
  for (const file of $("file-input").files) addFileToAttachments(file);
  $("file-input").value = "";  // 允许重复选择同一个文件
}

// 粘贴：截图后直接 Ctrl/⌘+V 到输入框；只拦截文件类内容，纯文本粘贴不受影响
function onPaste(e) {
  const items = e.clipboardData?.items;
  if (!items) return;
  let hasFile = false;
  for (const item of items) {
    if (item.kind === "file") {
      const file = item.getAsFile();
      if (file) { hasFile = true; addFileToAttachments(file); }
    }
  }
  if (hasFile) e.preventDefault();  // 阻止图片二进制被当文本粘进输入框
}

// 拖拽：把文件拖到输入框区域即可添加
function bindDragAndDrop(el) {
  ["dragover", "dragenter"].forEach(ev =>
    el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach(ev =>
    el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.remove("dragging"); }));
  el.addEventListener("drop", (e) => {
    for (const file of e.dataTransfer?.files || []) addFileToAttachments(file);
  });
}

// 压缩分隔卡片：早期消息已被总结为摘要（完整历史仍在数据库，只是不再发给模型）。
// 不渲染成普通对话气泡——它不是谁说的话；点击可展开摘要原文，
// 用户需要时仍能查到"被压缩掉了什么"。
function compactCard(summary) {
  const d = document.createElement("details");
  d.className = "compact-divider";
  const s = document.createElement("summary");
  s.textContent = "⇕ 以上较早的对话已压缩为摘要";
  const pre = document.createElement("pre");
  pre.className = "compact-summary";
  pre.textContent = summary || "（摘要内容为空）";
  d.append(s, pre);
  return d;
}

// ---------- 图片灯箱：点气泡里的缩略图 → 全屏查看 + 复制 ----------
// 全局单例：任意消息（实时/历史/归档还原）里的 .msg-img 点击后都进这里。
// 复制优先 Clipboard API 的 image/png；失败降级为「已打开图片，可右键复制」。
function openLightbox(src) {
  closeLightbox();
  const box = document.createElement("div");
  box.className = "lightbox";
  const img = document.createElement("img");
  img.src = src;
  const actions = document.createElement("div");
  actions.className = "lightbox-actions";
  const hint = document.createElement("div");
  hint.className = "lightbox-hint";
  hint.textContent = "点击空白处或按 Esc 关闭";
  const copy = document.createElement("button");
  copy.textContent = "📋 复制图片";
  copy.addEventListener("click", async () => {
    try {
      // data URI → blob（png/jpeg 都转成 png 写剪贴板，应用通用）
      const blob = await (await fetch(src)).blob();
      await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
      copy.textContent = "✓ 已复制";
      setTimeout(() => (copy.textContent = "📋 复制图片"), 1500);
    } catch {
      // 剪贴板不可用（权限拒绝/非安全上下文/类型不支持）：退回提示手动复制
      hint.textContent = "自动复制不可用：可右键图片选择「复制图片」";
      hint.style.color = "rgba(255,255,255,.85)";
    }
  });
  const close = document.createElement("button");
  close.textContent = "✕ 关闭";
  close.addEventListener("click", closeLightbox);
  actions.append(copy, close);
  box.append(img, actions, hint);
  box.addEventListener("click", (e) => { if (e.target === box) closeLightbox(); });
  document.addEventListener("keydown", lightboxEsc);
  document.body.appendChild(box);
}
function lightboxEsc(e) { if (e.key === "Escape") closeLightbox(); }
function closeLightbox() {
  document.querySelector(".lightbox")?.remove();
  document.removeEventListener("keydown", lightboxEsc);
}

// 气泡里的消息图片统一走这里：带点击放大 + 复制
function msgImage(src) {
  const img = document.createElement("img");
  img.src = src;
  img.className = "msg-img clickable";
  img.title = "点击放大";
  img.addEventListener("click", () => openLightbox(src));
  return img;
}

// 带附件的用户气泡：文字 + 图片缩略图/文件名
function buildUserBubble(text, atts) {
  const div = document.createElement("div");
  div.className = "bubble user";
  if (text) {
    const t = document.createElement("div");
    t.textContent = text;
    div.appendChild(t);
  }
  for (const a of atts || []) {
    if (a.kind === "image" && a.preview) {
      div.appendChild(msgImage(a.preview));
    } else {
      const f = document.createElement("div");
      f.className = "att-file";
      f.textContent = "📄 " + a.name;
      div.appendChild(f);
    }
  }
  return div;
}

function userBubble(text, atts) {
  chatEl.appendChild(buildUserBubble(text, atts));
  chatEl.scrollTop = chatEl.scrollHeight;
}

// ---------- 模型：激活切换（工具栏气泡）+ 供应商管理（弹窗） ----------
let providers = [];        // 供应商列表缓存（含各自模型）
let activeModel = { provider_id: "", model: "" };
let activeModelVision = true;  // 激活模型是否支持看图（/api/config 提供）
let editingProvId = null;  // 管理面板当前打开的供应商（null = 新供应商未保存）
let editorModels = [];     // 管理面板里正在编辑的模型行

async function loadConfig() {
  try {
    const cfg = await api("/api/config");
    activeModel = { provider_id: cfg.provider_id, model: cfg.model };
    contextWindow = cfg.context_window || contextWindow;
    activeModelVision = !!cfg.vision;   // 激活模型能否看图（决定附件上传时的提示）
    lastCfg = cfg;                      // 设置面板展示用
    $("model-label").textContent = `${cfg.provider_name || "模型"} / ${cfg.model || "—"}`;
    updateCtxChip();
  } catch (e) {
    $("model-label").textContent = "模型未配置";
  }
}

async function loadModelPop() {
  const data = await api("/api/models");
  const list = $("model-pop-list");
  list.innerHTML = "";
  let lastProv = null;
  for (const m of data.models) {
    if (m.provider_id !== lastProv) {  // 按供应商分组显示
      lastProv = m.provider_id;
      const head = document.createElement("div");
      head.className = "mp-head";
      head.textContent = m.provider_name;
      list.appendChild(head);
    }
    const isActive = m.provider_id === activeModel.provider_id && m.model === activeModel.model;
    const row = document.createElement("div");
    row.className = "mp-row" + (isActive ? " active" : "");
    const name = document.createElement("span");
    name.textContent = m.model;
    const win = document.createElement("span");
    win.className = "mp-win";
    win.textContent = fmtWan(m.context_window);
    const check = document.createElement("span");
    check.className = "mp-check";
    check.textContent = isActive ? "✓" : "";
    row.append(name, win, check);
    row.addEventListener("click", async () => {
      try {
        const cfg = await api("/api/active-model", {
          method: "POST",
          body: JSON.stringify({ provider_id: m.provider_id, model: m.model }),
        });
        activeModel = { provider_id: cfg.provider_id, model: cfg.model };
        contextWindow = cfg.context_window || contextWindow;
        $("model-label").textContent = `${cfg.provider_name} / ${cfg.model}`;
        $("model-pop").classList.add("hidden");
        updateCtxChip();
        toast(`已切换到 ${cfg.provider_name} / ${cfg.model}`);
      } catch (e) {
        toast("切换失败：" + e.message);
      }
    });
    list.appendChild(row);
  }
  if (!data.models.length) {
    const empty = document.createElement("div");
    empty.className = "mp-empty";
    empty.textContent = "还没有可用模型，点下方「管理模型」添加";
    list.appendChild(empty);
  }
}

function toggleModelPop() {
  const pop = $("model-pop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  loadModelPop().then(() => {
    const rect = $("model-chip").getBoundingClientRect();
    pop.style.left = Math.max(8, rect.left) + "px";
    pop.style.bottom = (innerHeight - rect.top + 8) + "px";
    pop.classList.remove("hidden");
  }).catch((e) => toast(e.message));
}

// ---------- 管理模型（供应商 CRUD + 模型列表 + 测试链接） ----------
async function openProvModal() {
  $("model-pop").classList.add("hidden");
  $("prov-modal").classList.remove("hidden");
  providers = await api("/api/providers").catch(() => []);
  const valid = providers.some(p => p.id === editingProvId);
  openProvEditor(valid ? editingProvId : (providers[0]?.id ?? null));
}

function renderProvList() {
  const ul = $("prov-list");
  ul.innerHTML = "";
  for (const p of providers) {
    const li = document.createElement("li");
    li.className = "prov-item" + (p.id === editingProvId ? " active" : "");
    const dot = document.createElement("span");
    dot.className = "prov-dot " + (p.enabled ? "on" : "off");
    const name = document.createElement("span");
    name.textContent = p.name;
    li.append(dot, name);
    li.addEventListener("click", () => openProvEditor(p.id));
    ul.appendChild(li);
  }
}

function openProvEditor(pid) {
  editingProvId = pid;
  renderProvList();
  const p = providers.find(x => x.id === pid);
  $("p-name").value = p?.name || "";
  $("p-url").value = p?.base_url || "";
  $("p-format").value = p?.api_format || "openai";
  $("p-win").value = p?.context_window || 128000;
  $("p-key").value = "";
  $("p-key").placeholder = p?.api_key_masked ? `已保存（${p.api_key_masked}），留空不变` : "输入 API Key";
  $("p-enabled").checked = p ? p.enabled : true;
  $("p-test-result").textContent = "";
  $("p-delete").style.display = pid === "default" ? "none" : "";
  editorModels = p ? p.models.map(m => ({ ...m })) : [{ name: "", context_window: 262144, enabled: true }];
  renderModelRows();
}

function renderModelRows() {
  const box = $("p-models");
  box.innerHTML = "";
  editorModels.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "pm-row";
    const name = document.createElement("input");
    name.className = "mono pm-name";
    name.placeholder = "模型名";
    name.value = m.name;
    name.addEventListener("input", () => (editorModels[i].name = name.value));
    const win = document.createElement("input");
    win.className = "mono pm-win-input";
    win.placeholder = "窗口";
    win.title = "上下文窗口（token），用于容量显示";
    win.value = m.context_window;
    win.addEventListener("input", () => (editorModels[i].context_window = parseInt(win.value) || 262144));
    const en = document.createElement("input");
    en.type = "checkbox";
    en.checked = m.enabled;
    en.title = "启用（出现在聊天工具栏）";
    en.addEventListener("change", () => (editorModels[i].enabled = en.checked));
    const vision = document.createElement("input");
    vision.type = "checkbox";
    vision.checked = !!m.vision;
    vision.title = "视觉：该模型支持看图（非视觉模型发图时，由它代为识别）";
    vision.addEventListener("change", () => (editorModels[i].vision = vision.checked));
    const test = document.createElement("button");
    test.className = "pm-test";
    test.textContent = "⚡";
    test.title = "测试该模型是否连通";
    test.addEventListener("click", async () => {
      if (!editorModels[i].name.trim()) return toast("先填写模型名再测试");
      test.textContent = "…";
      test.className = "pm-test";
      try {
    const r = await api("/api/providers/test", {
      method: "POST",
      body: JSON.stringify({
        provider_id: editingProvId,
        base_url: $("p-url").value.trim(),
        api_format: $("p-format").value,
        api_key: $("p-key").value.trim(),  // 留空 = 用已保存的 Key
        model: editorModels[i].name.trim(),
      }),
    });
        test.textContent = r.ok ? "✓" : "✗";
        test.classList.add(r.ok ? "ok" : "err");
        test.title = r.ok ? `连通（${r.latency_ms}ms）` : summarize(r.error, 120);
      } catch (e) {
        test.textContent = "✗";
        test.classList.add("err");
        test.title = e.message;
      }
    });
    const del = document.createElement("button");
    del.className = "t-del big";
    del.textContent = "🗑";
    del.title = "删除模型";
    del.addEventListener("click", async () => {
      if (editingProvId && m.name) {
        try {
          await api("/api/providers/models/delete", {
            method: "POST",
            body: JSON.stringify({ provider_id: editingProvId, name: m.name }),
          });
        } catch (e) { /* 模型还没保存过，静默 */ }
      }
      editorModels.splice(i, 1);
      renderModelRows();
    });
    row.append(name, win, test, vision, en, del);
    box.appendChild(row);
  });
  if (!editorModels.length) {
    const empty = document.createElement("div");
    empty.className = "pm-empty";
    empty.textContent = "（还没有模型）";
    box.appendChild(empty);
  }
  // 列表底部的"添加模型"入口（动态重建，所以在这里挂事件）
  const addBtn = document.createElement("button");
  addBtn.id = "p-model-add";
  addBtn.className = "add-row";
  addBtn.textContent = "＋ 添加模型";
  addBtn.addEventListener("click", () => {
    editorModels.push({ name: "", context_window: 262144, enabled: true });
    renderModelRows();
  });
  box.appendChild(addBtn);
}

async function saveProv() {
  const name = $("p-name").value.trim();
  const base = $("p-url").value.trim();
  if (!name) { $("p-name").focus(); return toast("请填写供应商名称"); }
  if (!base) { $("p-url").focus(); return toast("请填写 Base URL"); }
  try {
    const r = await api("/api/providers/save", {
      method: "POST",
      body: JSON.stringify({
        id: editingProvId,
        name, base_url: base,
        api_format: $("p-format").value,
        api_key: $("p-key").value.trim(),   // 留空 = 保持已保存的 Key
        context_window: parseInt($("p-win").value) || undefined,  // 不填/非法 = 保持原值
        enabled: $("p-enabled").checked,
        models: editorModels.filter(m => m.name.trim()),
      }),
    });
    toast("已保存");
    editingProvId = r.id;
    providers = await api("/api/providers");
    renderProvList();
    openProvEditor(editingProvId);
    loadConfig();  // 若改的是激活模型，刷新工具栏显示
  } catch (e) {
    toast("保存失败：" + e.message);
  }
}

async function deleteProv() {
  if (!editingProvId) return;
  if (editingProvId === "default") return toast("默认供应商不可删除");
  try {
    await api("/api/providers/delete", { method: "POST", body: JSON.stringify({ id: editingProvId }) });
  } catch (e) {
    return toast(e.message);
  }
  toast("已删除供应商");
  providers = await api("/api/providers");
  editingProvId = providers[0]?.id ?? null;
  openProvEditor(editingProvId);
}

async function testProv() {
  const result = $("p-test-result");
  result.textContent = "测试中…";
  result.className = "status";
  try {
    const r = await api("/api/providers/test", {
      method: "POST",
      body: JSON.stringify({
        provider_id: editingProvId,
        base_url: $("p-url").value.trim(),
        api_format: $("p-format").value,
        api_key: $("p-key").value.trim(),  // 留空 = 用已保存的 Key
        model: (editorModels.find(m => m.name.trim()) || {}).name || "",
      }),
    });
    if (r.ok) {
      result.textContent = `✅ 连接成功（${r.latency_ms}ms）`;
      result.classList.add("ok");
    } else {
      result.textContent = "❌ " + summarize(r.error, 90);
      result.classList.add("err");
    }
  } catch (e) {
    result.textContent = "❌ " + e.message;
  }
}

// ---------- 工作区（按任务隔离：带 session_id 查/改该任务的；不带 = 用户默认） ----------
async function loadWorkspace() {
  try {
    const qs = currentSession ? `?session_id=${encodeURIComponent(currentSession)}` : "";
    const w = await api("/api/workspace" + qs);
    setWsLabel(w.path);
  } catch (e) { /* 忽略 */ }
}

function setWsLabel(path) {
  const seg = (path || "").replace(/\/+$/, "").split("/").pop() || path;
  $("ws-short").textContent = seg || "工作区";
  $("ws-pick").title = path || "";
}

let mCwd = "", mParent = null, mHome = "";

async function navTo(path) {
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  const info = await api("/api/fs/dirs" + qs);
  mCwd = info.path;
  mParent = info.parent;
  mHome = info.home;
  $("m-path").textContent = info.path;
  const ul = $("m-list");
  ul.innerHTML = "";
  for (const name of info.dirs) {
    const li = document.createElement("li");
    li.textContent = "📁 " + name;
    li.addEventListener("click", () => navTo((mCwd.endsWith("/") ? mCwd : mCwd + "/") + name));
    ul.appendChild(li);
  }
  if (!info.dirs.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "（没有子目录了）";
    ul.appendChild(li);
  }
  $("m-up").disabled = !info.parent;
}

async function openPicker() {
  $("modal").classList.remove("hidden");
  try {
    const cur = $("ws-pick").title;
    const parent = cur ? cur.replace(/\/[^/]+\/?$/, "") : "";
    await navTo(parent || undefined);
  } catch (e) {
    await navTo(undefined);
  }
}

async function chooseWorkspace() {
  try {
    const body = { path: mCwd };
    if (currentSession) body.session_id = currentSession;
    const w = await api("/api/workspace", { method: "POST", body: JSON.stringify(body) });
    setWsLabel(w.path);
    $("modal").classList.add("hidden");
    bubble("assistant", currentSession
      ? `（本任务的工作区已切换到 ${w.path}，之后我的文件操作和命令都在这个目录里进行；其他任务不受影响）`
      : `（已把 ${w.path} 设为新任务的默认工作区）`);
  } catch (e) {
    $("m-path").textContent = "切换失败：" + e.message;
  }
}

// ---------- 上下文容量 ----------
function updateCtxChip() {
  const chip = $("ctx-chip");
  if (!usageNow) { chip.textContent = "⛁ —"; return; }
  chip.textContent = `⛁ ${fmtWan(usageNow.prompt_tokens)} / ${fmtWan(contextWindow)}`;
}

async function refreshCtx() {
  if (!currentSession) { usageNow = null; updateCtxChip(); return; }
  try {
    const c = await api(`/api/context?session_id=${encodeURIComponent(currentSession)}`);
    contextWindow = c.window || contextWindow;
    usageNow = { prompt_tokens: c.tokens, context: c.breakdown, cache_hit_rate: c.cache_hit_rate };
    updateCtxChip();
  } catch (e) { /* 忽略 */ }
}

function toggleCtxPop() {
  const pop = $("ctx-pop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  renderCtxPop();
  const rect = $("ctx-chip").getBoundingClientRect();
  pop.style.left = Math.max(8, rect.left) + "px";
  pop.style.bottom = (innerHeight - rect.top + 8) + "px";
  pop.classList.remove("hidden");
}

function renderCtxPop() {
  const nums = $("ctx-nums"), fill = $("ctx-fill"), bd = $("ctx-breakdown");
  const tokens = usageNow ? usageNow.prompt_tokens : 0;
  const pct = Math.min(100, (tokens / contextWindow) * 100);
  nums.textContent = `${fmtWan(tokens)} / ${fmtWan(contextWindow)}（${pct.toFixed(1)}%）`;
  fill.style.width = pct + "%";
  fill.classList.toggle("warn", pct > 80);

  const labels = { system: "系统提示词", tools: "工具定义", user: "用户消息", assistant: "助手回复", tool_results: "工具结果" };
  bd.innerHTML = "";
  const b = (usageNow && usageNow.context) || {};
  const total = Math.max(1, Object.values(b).reduce((x, y) => x + (y || 0), 0));
  for (const [key, label] of Object.entries(labels)) {
    const row = document.createElement("div");
    row.className = "ctx-row";
    const name = document.createElement("span");
    name.textContent = label;
    const val = document.createElement("b");
    val.textContent = ((b[key] || 0) / total * 100).toFixed(1) + "%";
    row.append(name, val);
    bd.appendChild(row);
  }
  $("ctx-cache").textContent = usageNow && usageNow.cache_hit_rate != null ? usageNow.cache_hit_rate + "%" : "—";
}

// ---------- 常驻事件流（SSE 断线重连） ----------
// 「每轮一个流」改为「命令(POST 立即返回) + 常驻事件流(GET events)」：
// 回合过程（delta/done/…）全部从 events 通道到达。断线重连三件套：
//   * 浏览器 EventSource 断线自动重连，并自动携带 Last-Event-ID 头（服务端优先采用）；
//   * 每会话最近 seq 存 localStorage，页面刷新后以 ?since= 续接正在进行的回合；
//   * seq 闸门（lastSeq）：服务端补发可能带回本页已应用过的事件（正在进行的
//     回合会从 turn_start 起整段补发），按 seq 跳过——这是"不丢字不重复"的关键。
let es = null;               // 当前任务的 EventSource（每任务一条，切换任务时换）
let lastSeq = null;          // 本页已应用（渲染过）的最大事件 seq；null = 全新观看者
let bootBuffer = null;       // 补发段缓冲：caught_up 到达前无法判定回合完整性，先攒着
let myNonce = null;          // 本 tab 发出的当前回合 nonce：turn_start 不重复画自己的气泡

const seqKey = (sid) => `sse_seq_${sid}`;

function closeEvents() {
  if (es) { es.close(); es = null; }
  bootBuffer = null;
}

function openEvents(sid) {
  closeEvents();
  if (!sid) return;
  lastSeq = null;          // 新连接的 DOM 可能刚重建过：闸门清零，补发全量应用
  bootBuffer = [];         // 补发段先缓冲，caught_up 后一次性定性（见 finishBoot）
  const qs = [`token=${encodeURIComponent(authToken)}`];  // EventSource 带不了 Authorization 头
  const stored = localStorage.getItem(seqKey(sid));
  if (stored != null) qs.push(`since=${stored}`);
  es = new EventSource(`/api/sessions/${encodeURIComponent(sid)}/events?` + qs.join("&"));
  es.onmessage = onSSEEvent;
  // onerror 不额外处理：EventSource 会自动重连（携带 Last-Event-ID 头）；
  // 需要"放弃续接"的场景（缺口太大/服务重启）由服务端 resync 事件驱动。
  // 唯一例外：已登出还无限重连没有意义，这里直接停。
  es.onerror = () => { if (!authToken) closeEvents(); };
}

function onSSEEvent(e) {
  let evt;
  try { evt = JSON.parse(e.data); } catch { return; }
  const seq = e.lastEventId ? Number(e.lastEventId) : null;
  if (seq != null && !Number.isNaN(seq) && currentSession) {
    // 每条业务事件都续存 localStorage：页面随时刷新都能以最新 seq 续接
    localStorage.setItem(seqKey(currentSession), String(seq));
  }
  if (evt.type === "caught_up") return finishBoot(evt);
  if (evt.type === "resync") return handleResync(evt);
  // seq 闸门：跳过本页已应用过的事件（服务端为覆盖"刷新接上正在输出的回合"
  // 场景，会把进行中回合从 turn_start 起整段补发，与本页已渲染部分重叠）
  if (seq != null && lastSeq != null && seq <= lastSeq) return;
  if (bootBuffer !== null) { bootBuffer.push({ seq, evt }); return; }
  applyEvent(evt, seq);
}

// 补发段定性：把缓冲里的事件按回合分组，逐组决定渲染与否。
//  * 完整回合（…turn_end 俱全）且 turn_end.user_mid 已在时间线 → 该回合其实
//    早已落库（断线期间完成、刷新页面时历史已含它），跳过渲染，否则与时间线重复；
//  * 其余（时间线里没有的完整回合、以及没有 turn_end 的"进行中"尾巴）→ 渲染。
// 回合开头之前的孤立尾巴（补发从回合中段起）也按"隐式完整回合"处理：它必然
// 跟着一个 turn_end——进行中回合的 turn_start 一定被服务端一并补发（见
// 后端 replay_plan），不会出现悬空尾巴；turn_end.user_mid 同样能判定去留。
function finishBoot(caughtUp) {
  const buf = bootBuffer || [];
  bootBuffer = null;
  if (caughtUp && caughtUp.running === false && streaming) {
    // 连上时服务端已无进行中回合，而本页还挂在"生成中"：回合在断线/服务
    // 重启之间死掉了（未落盘）。手动收尾不挂起——手测②"刷新接上进行中
    // 回合"的正常路径不会走到这里（running=true）。
    setStreaming(false);
    clearInterval(metaTimer);
    if (liveBubble) {
      liveBubble.classList.remove("streaming");
      const note = document.createElement("div");
      note.className = "meta";
      note.textContent = "（连接中断，本回合未完成，输入未保存）";
      chatEl.appendChild(note);
    }
    toast("连接已恢复；中断的回合未保存，请重新发送");
  }
  const segments = [];  // 每段 = { events: [{seq,evt}...], userMid, closed }
  let cur = { events: [], userMid: null, closed: false };
  for (const item of buf) {
    if (item.evt.type === "turn_start") {
      segments.push(cur);           // 之前的孤立尾巴（若有）先封段
      cur = { events: [item], userMid: null, closed: false };
    } else {
      cur.events.push(item);
      if (item.evt.type === "turn_end") {
        cur.closed = true;
        cur.userMid = item.evt.user_mid;
        segments.push(cur);
        cur = { events: [], userMid: null, closed: false };
      }
    }
  }
  segments.push(cur);
  // 补发段里若有"已闭合且 user_mid 已在时间线"的回合，说明那个回合在离开
  // 期间其实正常完成了（历史里已经有它）——这不是中断，不能报"输入未保存"。
  const anyRecovered = segments.some(seg => seg.closed && seg.userMid && historyMids.has(seg.userMid));
  if (caughtUp && caughtUp.running === false && streaming && !anyRecovered) {
    // 连上时服务端已无进行中回合，而本页还挂在"生成中"：回合在断线/服务
    // 重启之间死掉了（未落盘）。手动收尾不挂起——手测②"刷新接上进行中
    // 回合"的正常路径不会走到这里（running=true）。
    setStreaming(false);
    clearInterval(metaTimer);
    if (liveBubble) {
      liveBubble.classList.remove("streaming");
      const note = document.createElement("div");
      note.className = "meta";
      note.textContent = "（连接中断，本回合未完成，输入未保存）";
      chatEl.appendChild(note);
    }
    toast("连接已恢复；中断的回合未保存，请重新发送");
  }
  for (const seg of segments) {
    if (!seg.events.length) continue;
    if (seg.closed && seg.userMid && historyMids.has(seg.userMid)) continue;  // 已在时间线
    for (const { seq: s, evt } of seg.events) applyEvent(evt, s);
  }
}

// resync：服务端判定缺口补不齐（缓冲被挤掉 / 服务重启内存清空）。
// 处理：断开自动重连 → 全量刷新时间线 → 以 resync.seq 为锚重连。重连后若
// 回合仍在进行，服务端会从回合 turn_start 起整段补发，自然接上；若已结束，
// 全量刷新的时间线已含它。绝不能停在重连里——缺口不会自愈。
function handleResync(evt) {
  if (es) { es.close(); es = null; }
  bootBuffer = null;
  lastSeq = null;        // 时间线即将整页重建：闸门清零
  if (currentSession) localStorage.setItem(seqKey(currentSession), String(evt.seq ?? 0));
  const keep = currentSession;
  currentSession = null;  // 绕过 switchSession 的同 id 早退
  switchSession(keep);
}

// ---------- 流式渲染：执行过程时间线 + 打字机回答 ----------
let liveBubble = null, metaEl = null, metaTimer = null, qStart = 0;
let thinkEl = null;  // 当前轮次的思考流块（思考模型的 reasoning_delta 实时显示用）
let traceEl = null, traceSteps = 0;
let pendingCalls = [];  // 已发出但未见结果的工具调用（算持续时长用）
let permissionCards = new Map();  // permission id -> 卡片元素：补发重放同一请求时复用/整卡重画，不叠卡片
let liveMsgs = new Map();  // mid -> {el, text}：事件流里同一 mid 的 delta 归并进同一气泡
let curMid = null;         // 当前回答段落的 mid（round 事件切换）
let historyMids = new Set();  // 已从分页接口加载进时间线的消息 mid（补发去重基准）

const TOOL_ICONS = {
  write_file: "✏️", apply_patch: "✏️",
  read_file: "🔍", grep: "🔍", list_dir: "📂",
  run_bash: "▶️", calculator: "🧮", current_time: "🕐", get_weather: "🌤️",
};

function ensureTrace() {
  if (traceEl) return;
  traceEl = document.createElement("details");
  traceEl.className = "trace";
  traceEl.open = true;  // 执行期间展开，实时看过程
  const summary = document.createElement("summary");
  summary.textContent = "执行过程";
  traceEl.appendChild(summary);
  chatEl.appendChild(traceEl);
}

function appendTrace(el) {
  ensureTrace();
  traceEl.appendChild(el);
  traceSteps += 1;
  traceEl.querySelector("summary").textContent = `执行过程（${traceSteps} 步）`;
  chatEl.scrollTop = chatEl.scrollHeight;
}

function traceLine(text) {
  const div = document.createElement("div");
  div.className = "trace-line";
  div.textContent = text;
  appendTrace(div);
}

function toolCallLine(name, argsStr) {
  let a = {};
  try { a = JSON.parse(argsStr); } catch { /* 参数不是 JSON */ }
  const main = summarize(a.command || a.path || a.expression || a.pattern || a.city || "", 46);
  const d = document.createElement("details");
  d.className = "tl";
  const summary = document.createElement("summary");
  summary.textContent = `${TOOL_ICONS[name] || "🔧"} ${name}${main ? " · " + main : ""}`;
  const pre = document.createElement("pre");
  pre.textContent = prettyJson(argsStr);
  d.append(summary, pre);
  appendTrace(d);
  pendingCalls.push({ name, el: d, t: Date.now() });
}

function toolResultLine(name, resultStr) {
  // 持续时长：配对最近一次同名调用
  let dur = "";
  const idx = pendingCalls.map(c => c.name).lastIndexOf(name);
  if (idx >= 0) {
    const call = pendingCalls.splice(idx, 1)[0];
    dur = ` · ${((Date.now() - call.t) / 1000).toFixed(1)}s`;
  }
  const d = document.createElement("details");
  d.className = "tl result";
  const summary = document.createElement("summary");
  const pre = document.createElement("pre");
  let parsed = null;
  try { parsed = JSON.parse(resultStr); } catch { /* 纯文本 */ }

  const hintSuffix = (p) => p.hint ? `\n💡 ${p.hint}` : "";

  if (parsed && typeof parsed === "object" && "exit_code" in parsed) {
    // run_bash：非零退出也带完整输出（统一信封下 ok:false 但输出是第一手材料）
    summary.textContent = `↩ ${dur.replace(" · ", "") || "0s"} · exit ${parsed.exit_code}`;
    if (parsed.exit_code !== 0) summary.classList.add("err");
    pre.textContent = [parsed.stdout, parsed.stderr].filter(Boolean).join("\n[stderr]\n")
      || "(无输出)";
    pre.textContent += hintSuffix(parsed);
  } else if (parsed && typeof parsed === "object" && "error" in parsed) {
    // 权限拒绝是"人做的决定"而非故障：单独标注，并带上给模型的改道提示
    const denied = typeof parsed.error === "string" && parsed.error.startsWith("权限拒绝");
    summary.textContent = denied ? `↩ 已拒绝${dur}` : `↩ 出错${dur}`;
    summary.classList.add("err");
    pre.textContent = [parsed.error, parsed.hint ? "💡 " + parsed.hint : ""].filter(Boolean).join("\n");
  } else if (parsed && typeof parsed === "object" && "added" in parsed) {
    summary.textContent = `↩ +${parsed.added} −${parsed.removed}${dur}`;
  } else if (parsed && typeof parsed === "object" && "lines" in parsed) {
    summary.textContent = `↩ 新建 ${parsed.lines} 行${dur}`;
  } else if (parsed && typeof parsed === "object" && "ok" in parsed && "result" in parsed) {
    // 统一信封的成功返回（read_file/grep/list_dir/…）：正文在 result 字段，
    // 直接展示正文而不是整坨 JSON
    const r = parsed.result;
    const flat = typeof r === "string" ? r : JSON.stringify(r, null, 2);
    summary.textContent = `↩ ${summarize(flat, 60)}${dur}`;
    pre.textContent = flat + hintSuffix(parsed);
  } else if (parsed) {
    summary.textContent = `↩ ${summarize(resultStr, 60)}${dur}`;
    pre.textContent = JSON.stringify(parsed, null, 2);
  } else {
    summary.textContent = `↩ ${summarize(resultStr, 60)}${dur}`;
    pre.textContent = resultStr;
  }
  d.append(summary, pre);
  appendTrace(d);
}

// 🔐 权限确认卡片：闸门命中 ask 时，回合暂停等用户三选一。
// 决定 POST /api/sessions/<sid>/permission/<pid>（事件到达时回合还没结束，
// streaming 状态保持，输入照常排队）。补发/重放会带来同一条 permission_request：
// 同一 id 整卡重画（覆盖旧卡），已答过的再答会得到 ok=false → 提示"已失效"。
async function decidePermission(evt, decision, noteEl, buttons) {
  for (const b of buttons) b.disabled = true;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(currentSession)}/permission/${encodeURIComponent(evt.id)}`,
      { method: "POST", body: JSON.stringify({ decision }) });
    if (r && r.ok === false) throw new Error(r.error || "确认已失效");
    noteEl.textContent = decision === "deny"
      ? "已拒绝：助手会收到拒绝原因并改用别的方案"
      : (decision === "allow_session" ? "已允许（本会话内同类操作不再询问）" : "已允许（仅本次）");
    noteEl.classList.add("resolved");
  } catch (e) {
    noteEl.textContent = "提交失败：" + e.message;
    noteEl.classList.add("resolved");
    for (const b of buttons) b.disabled = false;  // 可重试（如刷新后补发的旧卡重新生效前）
  }
}

function showPermissionCard(evt) {
  flushStreamBuffers();
  retireLiveBubble();
  ensureTrace();
  let card = permissionCards.get(evt.id);
  if (card) card.remove();  // 同一请求重放：整卡重画，绝不允许出现两张活卡
  card = document.createElement("div");
  card.className = "perm-card";
  const title = document.createElement("div");
  title.className = "perm-title";
  title.textContent = `🔐 权限确认 · ${evt.tool}`;
  const reason = document.createElement("div");
  reason.className = "perm-reason";
  reason.textContent = "触发原因：" + (evt.reason || "该操作需要确认");
  const pre = document.createElement("pre");
  pre.textContent = typeof evt.input === "string" ? evt.input : JSON.stringify(evt.input, null, 2);
  const btns = document.createElement("div");
  btns.className = "perm-btns";
  const mk = (label, decision, cls) => {
    const b = document.createElement("button");
    b.className = "perm-btn " + cls;
    b.textContent = label;
    b.onclick = () => decidePermission(evt, decision, note, [bOnce, bSession, bDeny]);
    return b;
  };
  const bOnce = mk("仅本次允许", "allow", "ok");
  const bSession = mk("本会话内允许", "allow_session", "ok");
  const bDeny = mk("拒绝", "deny", "no");
  btns.append(bOnce, bSession, bDeny);
  const note = document.createElement("div");
  note.className = "perm-note";
  note.textContent = "等待你的决定（5 分钟未确认将按拒绝处理）";
  card.append(title, reason, pre, btns, note);
  traceEl.appendChild(card);  // 不走 appendTrace：确认卡不算执行步骤
  permissionCards.set(evt.id, card);
  chatEl.scrollTop = chatEl.scrollHeight;
}

// ⏱ 耗时 + token 统计行（跟随当前回答气泡）
function metaText(elapsed, u) {
  let t = `⏱ ${elapsed}s`;
  if (u && u.total_tokens) t += ` · ↑${u.prompt_tokens} ↓${u.completion_tokens} tokens`;
  return t;
}

// 取到（或创建）mid 对应的回答气泡。mid 是回答段落的身份（服务端 round
// 事件分配）：同一 mid 的 delta 归并进同一气泡——这是"归并事件流中同一 mid
// 消息的增量"的落点，也是补发重放时能对上已有气泡的键。
function ensureLiveMsg(mid) {
  mid = mid || curMid || "_";
  let b = liveMsgs.get(mid);
  if (!b) {
    const el = document.createElement("div");
    el.className = "bubble assistant streaming";
    chatEl.appendChild(el);
    b = { el, text: "" };
    liveMsgs.set(mid, b);
    if (!metaEl) {
      metaEl = document.createElement("div");
      metaEl.className = "meta";
    }
    metaEl.textContent = metaText(((Date.now() - qStart) / 1000).toFixed(1), usageNow);
    chatEl.appendChild(metaEl);  // 已存在则移动到当前气泡后
  }
  liveBubble = b.el;  // 兼容既有的"当前气泡"语义（retire/done 收尾用）
  chatEl.scrollTop = chatEl.scrollHeight;
  return b;
}

// SSE 事件 → 页面更新（事件类型见 backend/app.py 的 _run_round）
function retireLiveBubble() {
  // 当前气泡"退役"：去掉打字机光标（否则中间轮次的气泡会一直闪），空的直接移除
  if (!liveBubble) return;
  liveBubble.classList.remove("streaming");
  if (!liveBubble.textContent) liveBubble.remove();
}

// 流式增量按帧合并：delta 到达频率远高于屏幕刷新率，逐条 textContent += 和
// scrollTop = scrollHeight 会各自强制一次重排，把主线程切碎——生成期间整个页面的
// 点击都会因此变迟钝。这里只攒增量（回答按 mid 分桶），requestAnimationFrame
// 每帧最多刷一次。
let pendingDeltas = new Map(), pendingThink = "", deltaFlushQueued = false;

function flushStreamBuffers() {
  deltaFlushQueued = false;
  if (pendingDeltas.size) {
    for (const [mid, text] of pendingDeltas) {
      const b = ensureLiveMsg(mid);
      b.text += text;
      b.el.textContent = b.text;
    }
    pendingDeltas.clear();
    chatEl.scrollTop = chatEl.scrollHeight;
  }
  if (pendingThink) {
    if (thinkEl) {
      thinkEl.textContent += pendingThink;
      thinkEl.scrollTop = thinkEl.scrollHeight;
    }
    pendingThink = "";
    chatEl.scrollTop = chatEl.scrollHeight;
  }
}

function queueStreamDelta(kind, mid, text) {
  if (kind === "answer") pendingDeltas.set(mid, (pendingDeltas.get(mid) || "") + text);
  else pendingThink += text;
  if (deltaFlushQueued) return;
  deltaFlushQueued = true;
  requestAnimationFrame(flushStreamBuffers);
}

// 事件应用（常驻事件流与补发段共用同一条路径——补发重放的就是当初的事件流）。
// 事件类型见 backend/app.py 的 _run_round：回合边界 turn_start/turn_end 是
// 新增的，其余与旧的每轮流式输出一致。
function applyEvent(evt, seq) {
  if (seq != null) lastSeq = seq;
  const t = evt.type;
  if (t === "turn_start") {
    // 回合开始。本 tab 自己发的消息（nonce 相同）不重复画气泡——发起方在
    // send/dispatch 时已带缩略图画过；其他标签页/刷新后的页面靠事件里的
    // 原文补画（附件只带名字：图片 base64 不该进环形缓冲占容量）
    if (!evt.nonce || evt.nonce !== myNonce) {
      userBubble(evt.input || "（仅附件）",
        (evt.atts || []).map(a => ({ kind: a.kind, name: a.name, preview: "" })));
    }
    // 回合级状态复位（原在 performSend 里；改为事件驱动后，刷新页面接上
    // 正在进行的回合也走同一套初始化）
    liveMsgs = new Map();
    liveBubble = null; traceEl = null; traceSteps = 0;
    metaEl = null; thinkEl = null; pendingCalls = [];
    permissionCards = new Map();  // 新回合的确认卡是新的请求：旧卡引用随时间线一起失效
    pendingDeltas = new Map(); pendingThink = "";
    usageNow = null; curMid = null; qStart = Date.now();
    clearInterval(metaTimer);
    metaTimer = setInterval(() => {
      if (metaEl && streaming) metaEl.textContent = metaText(((Date.now() - qStart) / 1000).toFixed(1), usageNow);
    }, 100);
    setStreaming(true);
    loadSessions();  // 新任务/新标题此刻才在服务端落定，列表刷新
  } else if (t === "round") {
    flushStreamBuffers();  // 上一轮的增量先落进旧气泡，再开新一轮
    traceLine(`🧠 思考 · 第 ${evt.round} 轮`);
    thinkEl = null;  // 新一轮的思考流开一个新块
    retireLiveBubble();
    curMid = evt.mid;  // 本轮回答段落的 mid：后续 delta/done 归并的键
  } else if (t === "reasoning_delta") {
    // 思考模型的推理过程实时流进「执行过程」面板当前轮次下方：
    // 思考阶段再长界面也有动静，不会再像假死；面板收起后不占聊天区
    if (!thinkEl) {
      ensureTrace();
      thinkEl = document.createElement("div");
      thinkEl.className = "think-line";
      traceEl.appendChild(thinkEl);  // 不走 appendTrace：思考流不算一步
    }
    queueStreamDelta("think", evt.mid, evt.delta);
  } else if (t === "answer_delta") {
    queueStreamDelta("answer", evt.mid, evt.delta);
  } else if (t === "tool_call") {
    flushStreamBuffers();
    retireLiveBubble();
    toolCallLine(evt.name, evt.arguments);
  } else if (t === "tool_result") {
    toolResultLine(evt.name, evt.result);
  } else if (t === "permission_request") {
    showPermissionCard(evt);
  } else if (t === "usage") {
    usageNow = evt;   // 供上下文气泡与统计行使用
    updateCtxChip();
    if (metaEl) metaEl.textContent = metaText(evt.elapsed_s, usageNow);
  } else if (t === "done") {
    pendingDeltas = new Map(); pendingThink = "";  // 完整回答直接覆盖，丢弃未刷的增量，防止 rAF 晚到追加旧文本
    const b = ensureLiveMsg(evt.mid);
    b.el.classList.remove("streaming");
    b.text = evt.answer;
    b.el.textContent = evt.answer;
    clearInterval(metaTimer);
    metaEl.textContent = metaText(evt.elapsed_s, evt.usage);
    if (evt.mid) historyMids.add(evt.mid);  // 已在屏上：防后续补发重复渲染
    if (traceEl) {
      traceEl.open = false;  // 执行完收起，保持对话清爽；点开可回看全过程
      traceEl.querySelector("summary").textContent = `执行过程（${traceSteps} 步 · ${evt.elapsed_s}s）`;
    }
    chatEl.scrollTop = chatEl.scrollHeight;
    loadSessions();  // 任务时间/排序刷新
  } else if (t === "compacted") {
    // 回答结束后的自动压缩（不产生回答流）：补一张分隔卡片并刷新容量显示。
    // 到达顺序在 done 之后——回答气泡已定稿，卡片插在对话流末尾即正确位置。
    chatEl.appendChild(compactCard(evt.summary));
    chatEl.scrollTop = chatEl.scrollHeight;
    usageNow = { prompt_tokens: evt.prompt_tokens, context: evt.context, cache_hit_rate: null };
    updateCtxChip();
    toast("早期对话已压缩为摘要，上下文占用已下降");
  } else if (t === "turn_end") {
    // 回合结束（服务端已落盘）：驱动排队队列推进的唯一信号——原来靠 POST
    // 收尾推进，现在 POST 立即返回，队列只能跟着回合生命周期走
    if (evt.user_mid) historyMids.add(evt.user_mid);
    clearInterval(metaTimer);
    setStreaming(false);
    myNonce = null;
    dispatchNextQueued();
  } else if (t === "history_renumbered") {
    // 服务端 ord 间隔耗尽兜底：整会话重编号过，before_ord 游标指向的旧序号
    // 在新序号空间里落在哪完全随机——继续翻页会漏条目或重复。重拉整个时间线
    // （绕过 switchSession 的同 id 早退）。罕见事件，整页重建的开销可接受。
    toast("历史序号已重排，正在刷新时间线");
    const keep = currentSession;
    currentSession = null;
    switchSession(keep);
  } else if (t === "session_deleted") {
    // 其他标签页删掉了这个任务：收摊回到新建态
    closeEvents();
    currentSession = null;
    resetStreamState();
    chatEl.innerHTML = "";
    welcome();
    loadSessions();
    toast("该任务已在其他窗口被删除");
  } else if (t === "error") {
    flushStreamBuffers();  // 已生成的部分内容留在气泡里，再显示错误
    retireLiveBubble();
    if (traceEl) traceEl.querySelector("summary").textContent = `执行过程（${traceSteps} 步 · 出错）`;
    bubble("assistant error", "❌ " + evt.message);
    // 生成中状态不在这里复位：turn_end 紧随 error 事件到达，由它统一收尾
  }
}

// 生成期间「发送」变身「停止」：点它请求服务端掐断当前生成。
// 按钮复位不在点击处——等 SSE 流送来 done/error 后，在 send() 的 finally 里。
function setStreaming(on) {
  streaming = on;
  const btn = $("send");
  btn.disabled = false;  // 生成中也要保持可点（此时点 = 停止）
  btn.textContent = on ? "■ 停止" : "发送";
  btn.classList.toggle("stop", on);
  inputEl.placeholder = on
    ? "生成中：现在输入将排队，回答完成后自动发送"
    : "输入问题或任务，Enter 发送（Shift+Enter 换行）";
}

// 离开当前会话视图（切换任务/新建任务/登出）时复位流式渲染状态。
// 这些都是全局单例，而回合结束信号（turn_end）只从事件流到达——旧会话的流
// 已随 closeEvents 断开，不复位的话 streaming 永远为 true：新会话里发消息
// 会被静默排队且永不派发，停止按钮也把停止请求发给错误的会话。
// 回合真身不丢：切回旧会话时由历史分页 + 补发定性（finishBoot）重建。
function resetStreamState() {
  clearInterval(metaTimer);
  setStreaming(false);
  myNonce = null;
  liveBubble = null; metaEl = null; thinkEl = null;
  traceEl = null; traceSteps = 0;
  liveMsgs = new Map(); pendingCalls = [];
  permissionCards = new Map();
  pendingDeltas = new Map(); pendingThink = "";
  curMid = null; usageNow = null;
}

async function stopGeneration() {
  $("send").textContent = "停止中…";
  try {
    await api("/api/chat/stop", { method: "POST", body: JSON.stringify({ session_id: currentSession }) });
  } catch (e) {
    // 停止指令没送到（比如服务刚重启）：不必处理，断流后 finally 会复位按钮
  }
}

// ---------- 发送与排队 ----------
// 生成期间再发消息：默认【排队】（当前回答完成后自动接着发），
// 队列卡片上可「⬆ 立即」（停止当前生成、马上执行这一条）/「✏ 编辑」/「🗑 删除」。
let pendingQueue = [];  // {text, payloadAtts, sessionId, el, immediate}

function send() {
  const text = inputEl.value.trim();
  if (!text && !attachments.length) return;
  if (attachments.some(a => !a.data)) {  // 占位附件还在读文件：等下一拍
    toast("附件还在读取中，请稍候一秒再发送");
    return;
  }
  const payloadAtts = attachments.map(a => ({ kind: a.kind, name: a.name, mime: a.mime, data: a.data }));
  const outAtts = attachments.map(a => ({ kind: a.kind, name: a.name, preview: a.preview }));
  inputEl.value = "";
  attachments = [];
  renderAttachTray();

  if (streaming) {
    // 排队：只显示队列卡片，正式气泡等派发执行时再渲染（否则会出现两条重复消息）
    queueMessage(text, payloadAtts);
    return;
  }
  userBubble(text, outAtts);
  performSend({ text, payloadAtts, sessionId: currentSession });
}

function queueMessage(text, payloadAtts) {
  const item = { text, payloadAtts, sessionId: currentSession, immediate: false, el: null };
  const wrap = document.createElement("div");
  wrap.className = "bubble user queued";
  const t = document.createElement("div");
  t.textContent = text || "（仅附件）";
  wrap.appendChild(t);
  for (const a of payloadAtts) {
    if (a.kind === "image") {
      wrap.appendChild(msgImage(`data:${a.mime};base64,${a.data}`));
    }
  }
  const actions = document.createElement("div");
  actions.className = "queue-actions";
  const mk = (label, fn, cls) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = "q-btn" + (cls ? " " + cls : "");
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    actions.appendChild(b);
  };
  mk("⬆ 立即", () => {
    item.immediate = true;
    const i = pendingQueue.indexOf(item);
    if (i > 0) { pendingQueue.splice(i, 1); pendingQueue.unshift(item); }  // 提到队首
    item.el?.remove();
    stopGeneration();  // 停掉当前生成；流结束后队列自动从队首开始发
  }, "q-immediate");
  mk("✏ 编辑", () => {
    pendingQueue.splice(pendingQueue.indexOf(item), 1);
    item.el?.remove();
    inputEl.value = item.text;
    for (const a of item.payloadAtts) {
      attachments.push({ kind: a.kind, name: a.name, mime: a.mime, data: a.data,
                         preview: a.kind === "image" ? `data:${a.mime};base64,${a.data}` : "" });
    }
    renderAttachTray();
    inputEl.focus();
  });
  mk("🗑 删除", () => {
    pendingQueue.splice(pendingQueue.indexOf(item), 1);
    item.el?.remove();
  });
  wrap.appendChild(actions);
  chatEl.appendChild(wrap);
  chatEl.scrollTop = chatEl.scrollHeight;
  item.el = wrap;
  pendingQueue.push(item);
}

function dispatchNextQueued() {
  if (streaming || !pendingQueue.length) return;
  const item = pendingQueue[0];
  // 用户已切到其他任务：先不发，切回来时再发（避免渲染混进别的对话视图）
  if (item.sessionId !== currentSession) return;
  pendingQueue.shift();
  item.el?.remove();  // 队列卡片退场，换成正式的已发送气泡
  const outAtts = item.payloadAtts.map(a => ({
    kind: a.kind, name: a.name,
    preview: a.kind === "image" ? `data:${a.mime};base64,${a.data}` : "",
  }));
  userBubble(item.text, outAtts);  // buildUserBubble 内部走 msgImage：可点放大
  performSend(item);
}

async function performSend(item) {
  // 命令接口：POST 立即返回，过程事件走常驻事件流。「生成中」状态在这里
  // 乐观置位（防连点双发——turn_start 事件到达前有一小段窗口），回合的真
  // 正结束由事件流的 turn_end 驱动（那里统一复位并推进队列）。
  setStreaming(true);
  // 回令：服务端会在 turn_start 里原样带回，本 tab 据此不重复画自己的气泡。
  // crypto.randomUUID 只在安全上下文可用（本机 http OK，局域网 http 不一定），
  // 手写兜底保证任何环境都能生成足够唯一的 nonce。
  item.nonce = item.nonce ||
    ((crypto.randomUUID ? crypto.randomUUID() : "") ||
     `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`);
  myNonce = item.nonce;
  const target = item.sessionId ?? currentSession;
  const body = JSON.stringify({
    message: item.text,
    nonce: item.nonce,
    attachments: item.payloadAtts,
  });
  try {
    if (target) {
      // 追问：REST 路径（任务已存在）
      await api(`/api/sessions/${encodeURIComponent(target)}/messages`, { method: "POST", body });
    } else {
      // 新任务的第一次发送：创建任务 + 入队一步完成
      const data = await api(`/api/sessions`, { method: "POST", body });
      currentSession = data.session_id;
      openEvents(currentSession);  // 立刻接事件流：turn_start 可能已在缓冲里等着补发
      loadWorkspace();             // 新任务按用户默认解析了自己的工作区，工具栏对齐
    }
  } catch (e) {
    // 命令没送出去（后端不可达/登录失效）：回合不会开始，本地复位。
    // 已发出的消息不放回队列（与旧行为一致：旧版 finally 里也是直接结束）
    bubble("assistant error", "❌ " + e.message);
    resetStreamState();
  }
}

// ---------- 事件绑定 ----------
// bind 带防崩保护：某个元素缺失（比如浏览器缓存了旧页面）只报一条 console 错误，
// 不再中断后面的绑定——否则一个 null 就能让所有按钮集体失灵。
function bind(id, event, fn) {
  const el = $(id);
  if (!el) {
    console.error(`[app.js] 页面上没有 #${id}，很可能是缓存了旧页面，请强制刷新（Cmd+Shift+R）`);
    return;
  }
  el.addEventListener(event, fn);
}

bind("input", "keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
bind("attach-btn", "click", () => $("file-input").click());
bind("file-input", "change", onFilesChosen);
bind("input", "paste", onPaste);
bindDragAndDrop(document.querySelector(".composer"));
bind("send", "click", () => (streaming ? stopGeneration() : send()));
bind("new-task", "click", newTask);
bind("model-chip", "click", toggleModelPop);
bind("manage-models", "click", openProvModal);
bind("prov-add", "click", addProv);
bind("prov-close", "click", () => $("prov-modal").classList.add("hidden"));
bind("p-save", "click", saveProv);
bind("p-test", "click", testProv);
bind("p-delete", "click", deleteProv);
bind("ws-pick", "click", openPicker);
bind("m-cancel", "click", () => $("modal").classList.add("hidden"));
bind("m-up", "click", () => mParent && navTo(mParent));
bind("m-home", "click", () => navTo(mHome || undefined));
bind("m-choose", "click", chooseWorkspace);
bind("ctx-chip", "click", toggleCtxPop);
bind("user-btn", "click", toggleUserPop);
bind("pop-logout", "click", logoutNow);
bind("up-manage", "click", () => {
  $("user-pop").classList.add("hidden");
  openProvModal();
});
bind("login-submit", "click", submitLogin);
bind("login-mode", "click", () => setLoginMode(loginMode === "login" ? "register" : "login"));
bind("login-user", "keydown", (e) => {
  if (e.key === "Enter") $("login-pass").focus();
});
bind("login-pass", "keydown", (e) => {
  if (e.key === "Enter") submitLogin();
});
// ---------- 左下角用户设置面板 ----------
let lastCfg = null;  // loadConfig 缓存，供设置面板展示模型信息

function toggleUserPop() {
  const pop = $("user-pop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  $("pop-name").textContent = who || "—";
  $("pop-avatar").textContent = (who || "牛").slice(0, 1);
  if (lastCfg) {
    $("up-model").textContent = lastCfg.model || "—";
    $("up-provider").textContent = lastCfg.provider_name || "—";
    $("up-window").textContent = fmtWan(lastCfg.context_window) + " tokens";
  }
  const rect = $("user-btn").getBoundingClientRect();
  pop.style.left = "10px";
  pop.style.bottom = (innerHeight - rect.top + 8) + "px";
  pop.classList.remove("hidden");
}

async function logoutNow() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* 掉线也继续本地登出 */ }
  $("user-pop").classList.add("hidden");
  showLogin();
}
document.addEventListener("click", (e) => {
  // 点弹窗外空白处关闭浮动层
  for (const [pop, btn] of [["ctx-pop", "ctx-chip"], ["model-pop", "model-chip"], ["user-pop", "user-btn"]]) {
    const el = $(pop);
    if (!el.classList.contains("hidden") && !el.contains(e.target) && !e.target.closest?.("#" + btn)) {
      el.classList.add("hidden");
    }
  }
});

function addProv() {
  editingProvId = null;  // null = 新供应商，保存时后端生成 id
  renderProvList();
  $("p-name").value = "";
  $("p-url").value = "";
  $("p-format").value = "openai";
  $("p-win").value = 128000;
  $("p-key").value = "";
  $("p-key").placeholder = "输入 API Key";
  $("p-enabled").checked = true;
  $("p-test-result").textContent = "";
  $("p-delete").style.display = "none";
  editorModels = [{ name: "", context_window: 262144, enabled: true }];
  renderModelRows();
  $("p-name").focus();
}

// ---------- 启动 ----------
function boot() {
  // 登录成功（或刷新后 token 仍有效）后的页面初始化；切用户时先清现场
  currentSession = null;
  chatEl.innerHTML = "";
  historyMids = new Set();  // 上一个用户/任务的去重基准作废
  $("login-page").classList.add("hidden");   // 离开登录页
  $("layout").classList.remove("hidden");    // 进入对话页
  setWho(who);
  welcome();
  loadConfig();
  loadWorkspace();
  loadSessions();
}

(async () => {
  if (!authToken) return showLogin();
  try {
    await api("/api/auth/me");   // 校验本地 token 是否仍有效
  } catch {
    return;   // 失效：api() 已把登录层弹出来
  }
  boot();
})();
refreshCtx();
