/* 前端逻辑：任务列表、常驻事件流对话、执行过程时间线、模型/工作区/上下文工具栏。
   原生 JS，无框架、无构建步骤。 */

const $ = (id) => document.getElementById(id);
const chatEl = $("chat");
const inputEl = $("input");

// ---------- 设备模式（桌面 / 手机） ----------
// 判定逻辑与 html[data-mode] 由 mode.js 负责（<head> 里先于本文件同步加载，
// 保证首帧渲染就带对模式），这里只读，不重复判定规则。
function isMobileMode() {
  const m = document.documentElement.dataset.mode;
  if (m) return m === "mobile";
  return window.CodingAgentMode ? window.CodingAgentMode.isMobile() : window.innerWidth <= 720;
}

// 输入框提示语随模式切换：手机没有 Shift 键，写"Shift+Enter 换行"只会让人困惑
const INPUT_PLACEHOLDER = {
  desktop: "输入问题或任务，Enter 发送（Shift+Enter 换行）",
  mobile: "输入问题或任务，点发送",
};
function applyPlaceholder() {
  if (!inputEl) return;
  inputEl.placeholder = INPUT_PLACEHOLDER[isMobileMode() ? "mobile" : "desktop"];
}
applyPlaceholder();
document.addEventListener("modechange", applyPlaceholder);

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
// Markdown 渲染引擎在 md.js（marked+DOMPurify+hljs，加载顺序见 index.html）；
// 这里只保留调用方。小工具（格式化/JSON 美化等）继续在本文件。

// 回答气泡：assistant 走 Markdown 渲染，user/error/note 保持纯文本。
// 纯文本路径用 textContent —— 与旧行为逐字节一致，不引入任何回归。
function buildBubble(className, text) {
  const div = document.createElement("div");
  div.className = `bubble ${className}`;
  if (className === "assistant") {
    div.classList.add("md");
    div.appendChild(renderMarkdown(text));
  } else {
    div.textContent = text;
  }
  return div;
}

// 就地重绘一个已存在的回答气泡（流式 textContent → 定稿 Markdown）。
// 加 .md 类是为了让 Markdown 相关的排版样式生效（气泡本身仍是 .bubble.assistant）。
function renderIntoBubble(el, text) {
  el.classList.add("md");
  el.replaceChildren(renderMarkdown(text));
}

// ---------- 贴底滚动（stick-to-bottom） ----------
// 流式输出时每帧都强制拉底的话，用户往上滑看历史会被拽回来（"卡卡的、
// 优先流式输出"的体感就来自这里）。规则：只有用户本来就在底部附近（80px）
// 才跟随滚动；往上滑了就不打扰，滚回底部或用户自己发消息时恢复跟随。
let stickBottom = true;
// 触顶自动加载更早的历史：滚到顶部附近（<120px）且还有更多时自动翻页，
// 不再依赖手动点「加载更早的消息」按钮（按钮保留，作触发的兜底入口）。
// histLoading 防重入：翻页请求在途时忽略后续 scroll 触发，加载完成或
// 没有更多时自动解除。
let histLoading = false;
chatEl.addEventListener("scroll", () => {
  stickBottom = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 80;
  updateBackBottom();
  if (!histLoading && histHasMore && chatEl.scrollTop < 120 && currentSession) {
    histLoading = true;
    loadHistoryPage().finally(() => { histLoading = false; });
  }
}, { passive: true });

function scrollBottom(force = false) {
  if (!force && !stickBottom) return;  // 用户在看历史：不拽
  chatEl.scrollTop = chatEl.scrollHeight;
}

// 「↓ 最新」悬浮按钮：贴底时隐藏，上滚超过一屏的 1/4 才出现（阈值太低会
// 在正常流动中闪烁）。点击平滑回底并恢复跟随。新内容到达时 scrollBottom
// 依旧只服务贴底用户——召回靠这个按钮，不靠强拽。
function updateBackBottom() {
  const btn = $("back-bottom");
  if (!btn) return;
  const away = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight;
  btn.classList.toggle("hidden", stickBottom || away < chatEl.clientHeight * 0.25);
}

function bubble(className, text) {
  const div = buildBubble(className, text);
  chatEl.appendChild(div);
  scrollBottom(true);  // 出现的新消息（错误/提示）永远贴底
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
  // 按自然日差算（去掉时分秒再相减）："1天前"其实可能是昨天 23:59 的任务
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((day(now) - day(d)) / 86400000);
  if (diffDays === 1) return `昨天 ${hm}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;  // 前天起就显示日期
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 2500);
}

// ---------- 任务列表：按项目（工作区）分组 + 折叠 + 重命名 ----------
let confirmingDelete = null;  // 正处于"确认删除"状态的任务 id（二次确认，防误触）
let sessionsCache = [];       // 最近一次拉取的任务列表，删除的乐观更新直接改它
let renamingSession = null;   // 正在重命名的任务 id（行内出现输入框）
const collapsedGroups = new Set();  // 已折叠的项目组（存组名）

async function loadSessions() {
  try {
    const list = await api("/api/sessions");
    sessionsCache = Array.isArray(list) ? list : [];
    renderSessions(sessionsCache);
  } catch (e) { /* 启动时后端未就绪不打扰 */ }
}

// 列表状态轮询：state 是服务端内存态，只在"当前会话"的回合边界事件里刷新
// 列表是不够的——别的会话在别处跑完时本页看不到。低频轮询兜住这个缺口；
// 页面隐藏（切标签/最小化）时跳过，不白费请求，回到前台立刻补一次。
let statePollTimer = null;
function startStatePolling() {
  if (statePollTimer) return;
  statePollTimer = setInterval(() => {
    if (!document.hidden) loadSessions();
  }, 8000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadSessions();
  });
}

function removeSessionLocal(id) {
  // 乐观更新：点击"删除"瞬间先在本地移除该行（请求后台进行），失败再回滚刷新。
  // 原先要等 DELETE + 列表刷新两个往返都回来 UI 才动，页面忙时会被感知成"点了没反应"。
  sessionsCache = sessionsCache.filter(s => s.id !== id);
  renderSessions(sessionsCache);
}

async function submitRename(id, title) {
  title = (title || "").trim();
  if (!title) { renamingSession = null; renderSessions(sessionsCache); return; }
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/rename`,
      { method: "POST", body: JSON.stringify({ title }) });
    const s = sessionsCache.find(x => x.id === id);
    if (s) s.title = title;
  } catch (e) { toast("重命名失败：" + e.message); }
  renamingSession = null;
  renderSessions(sessionsCache);
}

// 任务状态徽标：state 来自 GET /api/sessions（后端内存态现算）。
// none / 未知值返回 null（不占位）——"从没跑过"和"老后端不返回 state"都
// 退化成原样，不画空点。其余返回一个小圆点元素。
function stateBadge(state) {
  const meta = {
    running: { cls: "running", text: "●", title: "运行中" },
    waiting: { cls: "waiting", text: "● 等待确认", title: "等待你确认权限" },
    done: { cls: "done", text: "●", title: "已完成" },
    error: { cls: "error", text: "●", title: "上一轮出错" },
  }[state];
  if (!meta) return null;
  const el = document.createElement("span");
  el.className = "t-state " + meta.cls;
  el.textContent = meta.text;
  el.title = meta.title;
  return el;
}

function taskRow(s, list) {
  const li = document.createElement("li");
  li.className = "task" + (s.id === currentSession ? " active" : "");

  // 重命名状态：标题位置换成输入框（Enter 确认 / Esc 或失焦取消）
  if (s.id === renamingSession) {
    const inp = document.createElement("input");
    inp.className = "t-rename";
    inp.value = s.title || "";
    const done = () => submitRename(s.id, inp.value);
    inp.addEventListener("keydown", (e) => {
      // IME 组合态：Enter 交给输入法选词，别当确认（否则拼音没上屏就提交了）
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter") done();
      if (e.key === "Escape") { renamingSession = null; renderSessions(sessionsCache); }
    });
    inp.addEventListener("blur", () => { if (renamingSession === s.id) done(); });
    inp.addEventListener("click", (e) => e.stopPropagation());
    li.appendChild(inp);
    return li;
  }

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
    return li;
  }

  const title = document.createElement("div");
  title.className = "t-title";
  title.textContent = s.title || "新任务";
  const time = document.createElement("span");
  time.className = "t-time";
  time.textContent = fmtTime(s.updated);
  // 状态徽标：只在非 idle 时出现（idle 就是平时的样子，不画点免得满屏灰）。
  // 不只靠颜色区分——running 用会呼吸的圆点，waiting 额外给文字，色盲也可辨。
  const state = stateBadge(s.state);
  const rename = document.createElement("button");
  rename.className = "t-del";
  rename.textContent = "✏️";
  rename.title = "重命名";
  rename.addEventListener("click", (e) => {
    e.stopPropagation();
    renamingSession = s.id;
    renderSessions(sessionsCache);
    const inp = document.querySelector(".t-rename");
    if (inp) { inp.focus(); inp.select(); }
  });
  const del = document.createElement("button");
  del.className = "t-del";
  del.textContent = "🗑";
  del.title = "删除任务";
  del.addEventListener("click", (e) => {
    e.stopPropagation();
    confirmingDelete = s.id;   // 第一次点：只进入确认状态，不真删
    renderSessions(list);
  });
  // 注意 state 可能是 null（idle 无徽标）：appendChild(null) 会插入字面量
  // "null"，必须过滤掉空值再 append。
  li.append(...[title, state, time, rename, del].filter(Boolean));
  li.title = s.title || "";
  li.addEventListener("click", () => {
    confirmingDelete = null;
    switchSession(s.id);
  });
  return li;
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
  // 按项目分组：workspace 非空 → 组名 = 目录名；否则进"其他"。
  // 组内保持 updated 降序（接口已排好，稳定的分组遍历不破坏次序）。
  const groups = new Map();  // 组名 → sessions（保持原有顺序）
  for (const s of list) {
    const name = s.workspace ? (s.workspace.replace(/\/+$/, "").split("/").pop() || "项目") : null;
    const key = name || "__other__";
    if (!groups.has(key)) groups.set(key, { label: name || "其他", items: [] });
    groups.get(key).items.push(s);
  }
  const groupHeader = (label, count, key) => {
    const head = document.createElement("li");
    head.className = "group-head";
    const collapsed = collapsedGroups.has(key);
    const icon = key === "__other__" ? "💬" : "📁";
    const text = document.createElement("span");
    text.className = "gh-label";
    text.textContent = `${collapsed ? "▸" : "▾"} ${icon} ${label}（${count}）`;
    text.addEventListener("click", () => {
      collapsed ? collapsedGroups.delete(key) : collapsedGroups.add(key);
      renderSessions(sessionsCache);
    });
    head.appendChild(text);
    // 项目组头部「＋」：新建一个直接绑定该项目的任务（首条消息建会话时原子绑定）
    if (key !== "__other__") {
      const g = groups.get(key);
      const add = document.createElement("button");
      add.className = "gh-add";
      add.textContent = "＋";
      add.title = `在 ${label} 里新建任务`;
      add.addEventListener("click", (e) => {
        e.stopPropagation();
        newTask(g.items[0].workspace);  // 组内任务的 workspace 即该项目的绝对路径
      });
      head.appendChild(add);
    }
    return head;
  };
  // 项目组在前（按名排序），"其他"固定垫底
  const keys = [...groups.keys()].filter(k => k !== "__other__").sort(
    (a, b) => groups.get(a).label.localeCompare(groups.get(b).label, "zh"));
  if (groups.has("__other__")) keys.push("__other__");
  const onlyOneGroup = keys.length === 1;
  for (const key of keys) {
    const g = groups.get(key);
    // 只有一个组且是"其他"（全场都没有项目）时不显示组头——列表退化为平铺
    if (!(onlyOneGroup && key === "__other__")) ul.appendChild(groupHeader(g.label, g.items.length, key));
    if (collapsedGroups.has(key) && !onlyOneGroup) continue;
    for (const s of g.items) ul.appendChild(taskRow(s, list));
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
    railItems = [];
    rebuildRail();  // 当前任务被删：导航条收起
    refreshCtx();
    closeEvents();
    historyMids = new Set();
    loadConfig();  // 任务没了：标签回到全局默认（新任务的初始模型）
  }
  loadSessions();  // 与服务端对齐一次（时间戳/排序），不阻塞交互
}

async function newTask(presetProject = null) {
  // presetProject = 项目组头「＋」传入的绝对路径：新建即绑定该项目
  saveDraft(currentSession);   // 离开当前会话前存草稿
  currentSession = null;
  confirmingDelete = null;
  pendingProjectPath = presetProject;
  loadConfig();  // 回到新建态：标签显示全局默认（新任务将用的初始模型）
  resetStreamState();  // 旧任务的事件流已断，流式状态必须随之复位
  closeEvents();      // 旧任务的事件流断开：新任务未建，第一条消息发出后再连
  chatEl.innerHTML = "";
  welcome();
  railItems = [];
  rebuildRail();       // 新任务时间线为空：导航条收起
  restoreDraft(null);  // 新任务自己的草稿位（__new__）
  resetDocsPanel();    // 新任务态：收起文档栏并清空内容
  loadDocsList();      // 新任务态：文档计数清零
  resetAttachPop();    // 新任务态：附件浮窗收起、计数清零
  usageNow = null;
  updateCtxChip();
  if (presetProject) {
    // 直接绑定项目的新任务：工具栏立刻显示项目名（绑定发生在首条消息建会话时）
    wsCustom = true;
    setWsLabel(presetProject);
    renderWsChip();
    inputEl.focus();
  } else {
    wsCustom = false;
    loadWorkspace();  // 回到"新任务"态：工具栏显示「选择项目」
  }
  renderWsChip();
  refreshGitChip();    // 分支徽章随项目预选立即更新（新任务态也能显示目标项目的分支）
  await loadSessions();  // 重新拉取列表：旧任务仍显示，只是没有选中项；首条消息后新任务才出现
}
// ---------- 输入框草稿：按会话归属 ----------
// 输入框与附件托盘都是全局唯一的 DOM/内存状态，切换会话时若不处理，A 会话打的字、
// 挂的图会原样留在框里、看起来像"被带到了 B 会话"（截图里的 image.png 就是这么来的）。
// 文本草稿按会话 id 存 localStorage；附件是内存对象（含 base64，动辄几 MB），
// 写 localStorage 会撑爆配额，所以按会话 id 存在内存表里，随会话切换存/取。
// 新任务用 __new__ 占位，语义与文本草稿一致。
const DRAFT_NEW = "__new__";
const draftKey = (sid) => `draft_${sid || DRAFT_NEW}`;

function saveDraft(sid) {
  const text = inputEl.value;
  if (text) localStorage.setItem(draftKey(sid), text);
  else localStorage.removeItem(draftKey(sid));  // 空草稿不留残留，避免下次误恢复
  saveAttachDraft(sid);                          // 附件与文本同一归属，一起存
}

function restoreDraft(sid) {
  inputEl.value = localStorage.getItem(draftKey(sid)) || "";
  restoreAttachDraft(sid);                       // 并恢复该会话自己的附件托盘
}

// 边打字边存草稿：防抖 300ms，避免每个按键都写 localStorage。
let draftTimer = null;
inputEl.addEventListener("input", () => {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => saveDraft(currentSession), 300);
});

// ---------- 会话缩略导航条（minimap rail） ----------
// 只画【用户提问】：条目来自服务端返回的全量 user_index（mid+ord），因此进入
// 会话即显示整个会话的提问分布，不受"时间线只加载最近 N 条"的窗口限制。
// 每条横线点击时：已在 DOM 里就直接滚动定位，否则先按 around_ord 把那一页
// 历史加载进来再定位（精确跳转）。横线外观统一，不区分已加载/未加载。
const railEl = $("msg-rail");
let railItems = [];      // 全量用户提问索引 [{mid, ord}]（服务端给，按 ord 升序）
let railTicks = [];      // [{el, item}]：与 railItems 一一对应的横线元素
let railScheduled = false;

// 给一个消息节点打锚（幂等）：mid 用于跳转查找，role 用于 rail 过滤与样式。
// 返回原节点，方便在 return 语句里链式包一层。
function railTag(node, mid, role) {
  if (!node || !node.classList || !node.classList.contains("bubble") &&
      !node.classList.contains("artifact")) return node;
  if (mid) node.dataset.mid = mid;
  if (role) node.dataset.role = role;
  return node;
}

// 按 mid 在已加载的 DOM 里找用户气泡；没加载过则返回 null。
// 历史回放的用户消息可能包在 display:contents 的 holder 里，故用 querySelector 下钻。
function railFindNode(mid) {
  if (!mid) return null;
  const esc = (window.CSS && CSS.escape) ? CSS.escape(mid) : mid.replace(/"/g, '\\"');
  const inner = chatEl.querySelector(`[data-role="user"][data-mid="${esc}"]`);
  if (inner) return inner;
  // 兜底：mid 未回填时，按已加载用户气泡的顺序与索引位置近似对应
  return null;
}

// 重建导航条：条目 = 全量用户提问索引；为空（新会话/无提问）时整条隐藏。
function rebuildRail() {
  if (!railEl) return;
  railEl.innerHTML = "";
  railTicks = [];
  if (!railItems.length) { railEl.classList.add("hidden"); return; }
  railEl.classList.remove("hidden");
  const frag = document.createDocumentFragment();
  for (const item of railItems) {
    const tick = document.createElement("div");
    tick.className = "rail-tick";
    tick.title = railPreview(item);
    tick.addEventListener("click", () => jumpToItem(item));
    frag.appendChild(tick);
    railTicks.push({ el: tick, item });
  }
  railEl.appendChild(frag);
  syncRailActive();
}

// 条目摘要（hover 提示）：优先取已加载气泡的文本，未加载则显示序号提示。
function railPreview(item) {
  const node = railFindNode(item.mid);
  if (node) {
    const text = (node.textContent || "").replace(/\s+/g, " ").trim();
    if (text) return text.length > 60 ? text.slice(0, 60) + "…" : text;
  }
  return `第 ${railItems.indexOf(item) + 1} 条提问（点击定位）`;
}

// 滚动高亮：取视口纵向中线所在的那条已加载提问，对应横线加深。
// 未加载的条目按 ord 比例估算，保证横线高亮不跳空。
function syncRailActive() {
  if (!railTicks.length) return;
  const mid = chatEl.scrollTop + chatEl.clientHeight / 2;
  let active = 0;
  let best = -Infinity;
  railTicks.forEach((t, i) => {
    const node = railFindNode(t.item.mid);
    if (!node) return;
    if (node.offsetTop <= mid && node.offsetTop > best) { best = node.offsetTop; active = i; }
  });
  if (best === -Infinity) {
    // 没有任何已加载提问在视口上方：按滚动比例粗定位，避免高亮停在第 0 条
    const pct = chatEl.scrollHeight > chatEl.clientHeight
      ? chatEl.scrollTop / (chatEl.scrollHeight - chatEl.clientHeight) : 0;
    active = Math.round(pct * (railTicks.length - 1));
  }
  railTicks.forEach((t, i) => t.el.classList.toggle("active", i === active));
}

// 跳转：已在 DOM 里直接滚动；否则先加载该 ord 所在的一页历史，再滚动定位。
async function jumpToItem(item) {
  let node = railFindNode(item.mid);
  if (!node) {
    await loadWindowAround(item.ord);
    node = railFindNode(item.mid);
  }
  if (!node) { toast("这条消息还没加载出来，请稍后重试"); return; }
  const top = Math.max(0, node.offsetTop - chatEl.clientHeight / 3);
  chatEl.scrollTo({ top, behavior: "smooth" });
  node.classList.remove("rail-hit");
  void node.offsetWidth;          // 强制重排，让动画能重复触发
  node.classList.add("rail-hit");
  setTimeout(() => node.classList.remove("rail-hit"), 3000);
}

// 加载目标 ord 所在的一页（around_ord）：把该窗口的历史插进时间线。
// 取回的是窗口内 mid/ord/role 索引，正文仍由常规分页接口提供——这里按窗口
// 下界做一次 before_ord 分页，把整页拉回来（复用手头已有的渲染路径）。
async function loadWindowAround(ord) {
  try {
    const qs = new URLSearchParams({ around_ord: String(ord), limit: "50" });
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}/messages?` + qs);
    const win = data.window || [];
    if (!win.length) return;
    // 窗口下界之前的一页：保证目标条之前的上下文连续（向上翻页入口也能用）
    const lower = win[0].ord;
    const page = await api(`/api/sessions/${encodeURIComponent(currentSession)}/messages?` +
      new URLSearchParams({ before_ord: String(lower), limit: "100" }));
    // 目标所在的那一段必须也进 DOM，否则 railFindNode 永远找不到目标 mid。
    // 注意：around_ord 返回的 window 每条只有 {mid, ord, role}——【没有正文、
    // tool_calls、stats、trace】，它只是用来定位窗口边界的索引。绝不能把 win 直接
    // 丢进渲染列表：那样每条都会因 content 为空渲染成空气泡（.bubble 的 padding
    // 会撑出一个白色圆角方块），且 role="tool" 的条目也会漏进时间线——这正是
    // 「点定位条冒出一堆空白方块」的根因。
    // 正确做法：拿窗口上界再走一次常规分页，由后端完成与首屏一致的过滤与字段组装。
    const upper = win[win.length - 1].ord;
    const page2 = await api(`/api/sessions/${encodeURIComponent(currentSession)}/messages?` +
      new URLSearchParams({ before_ord: String(upper + 1), limit: "100" }));
    // 合并两段、按 ord 升序、按 mid 去重（避免与已加载区间重叠重复渲染）。
    const merged = [...(page.messages || []), ...(page2.messages || [])];
    const seen = new Set();
    const toAdd = [];
    for (const m of merged) {
      if (m.mid && (historyMids.has(m.mid) || seen.has(m.mid))) continue;
      if (m.mid) seen.add(m.mid);
      toAdd.push(m);
    }
    toAdd.sort((a, b) => (a.ord ?? 0) - (b.ord ?? 0));
    insertHistoryBefore(toAdd);
    // 同步向上翻页游标：插入了更早的消息后，histOldestOrd 要跟着前移，
    // 否则后续「加载更早」会用错误游标漏读或重复。
    if (toAdd.length && (histOldestOrd == null || toAdd[0].ord < histOldestOrd)) {
      histOldestOrd = toAdd[0].ord;
    }
    histHasMore = !!page.has_more || (page.messages || []).length > 0;
    updateLoadOlder();
    scheduleRail();
  } catch (e) {
    console.error("加载目标消息窗口失败", e);
  }
}

// 把一页历史插到时间线的最前面（复用向上翻页的插入位置与滚动补偿逻辑）。
function insertHistoryBefore(messages) {
  if (!messages.length) return;
  const frag = document.createDocumentFragment();
  for (const m of messages) {
    if (m.mid) historyMids.add(m.mid);
    frag.appendChild(historyNode(m));
  }
  const btn = $("load-older");
  const prevHeight = chatEl.scrollHeight, prevTop = chatEl.scrollTop;
  if (btn) btn.after(frag); else chatEl.insertBefore(frag, chatEl.children[1] || null);
  chatEl.scrollTop = prevTop + (chatEl.scrollHeight - prevHeight);
}

// 拉取最新的全量用户提问索引（回合结束后调用：本轮提问此刻才落库）。
async function refreshUserIndex() {
  if (!currentSession) return;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}/messages?limit=1`);
    if (Array.isArray(data.user_index)) { railItems = data.user_index; scheduleRail(); }
  } catch (e) { /* 静默：导航条不是关键路径，失败不影响对话 */ }
}

// 合并短时间内的多次重建请求（流式期间 done/turn_end 会连着触发）。
function scheduleRail() {
  if (railScheduled) return;
  railScheduled = true;
  requestAnimationFrame(() => { railScheduled = false; rebuildRail(); });
}

// 回填「最后一个还没打锚的用户气泡」的 mid：本 tab 自己发消息时 mid 未知，
// 服务端在 turn_end 才给。锚点回填后 rail 才认得出这条消息。
function tagLastUntaggedUser(mid) {
  const users = [...chatEl.querySelectorAll('[data-role="user"]')];
  const last = users[users.length - 1];
  if (last && !last.dataset.mid) { last.dataset.mid = mid; scheduleRail(); }
}

chatEl.addEventListener("scroll", syncRailActive, { passive: true });

// ---------- 历史消息回放（分页加载 + 外置归档） ----------
let histOldestOrd = null;  // 已加载最旧一条消息的 ord（向上翻页游标）
let histHasMore = false;   // 其上是否还有更早的消息

// 清掉某会话的未读徽标（绿/红点）：告诉服务端"这个任务的结果我已经看过了"。
// 未读标记语义——回合在用户不在这个会话时跑完才亮徽标，切进去看过就清。
// fire-and-forget：失败不打扰（顶多是列表上多留一个点，下次切换会再试）。
function markSessionSeen(id) {
  if (!id) return;
  api(`/api/sessions/${encodeURIComponent(id)}/seen`, { method: "POST" }).catch(() => {});
}

async function switchSession(id) {
  if (id === currentSession) return;
  saveDraft(currentSession);     // 离开前：把输入框内容存进旧会话的草稿
  currentSession = id;
  markSessionSeen(id);            // 进入即视为已读：清掉该任务的未读徽标
  resetStreamState();  // 旧会话的事件流已断，流式状态必须随之复位
  chatEl.innerHTML = "";
  welcome();
  railItems = [];                 // 上一个会话的提问索引作废
  rebuildRail();                  // 时间线已清空：导航条随之收起（等历史装载后再出现）
  restoreDraft(id);              // 进入后：载入该会话自己的草稿（不串到别的会话）
  histOldestOrd = null;
  histHasMore = false;
  historyMids = new Set();        // 补发去重基准随任务重建
  await loadHistoryPage();        // 时间线先行：补发定性（finishBoot）要拿它比对
  openEvents(id);                 // 再接事件流：断线/刷新期间的回合靠 since 补发接上
  await loadSessions();
  await refreshCtx();
  await loadConfig(id);  // 模型随任务走：切换后工具栏标签跟着换成该任务的模型
  loadWorkspace();  // 每个任务有自己的工作区：切换后工具栏跟着换（内部顺带拉权限模式）
  closeGitPop();    // 工作区变了，旧的提交列表不再对应当前项目
  refreshGitChip(); // 按钮上的分支名随任务的工作区更新
  dispatchNextQueued();  // 切回有排队消息的任务时，接着把排队的发出去
  resetDocsPanel();      // 上一个会话的文档栏不留给新会话：收起并清空
  resetBrowserPanel();   // 浏览器栏同理：会话的浏览器画面是会话私有
  loadDocsList();        // 刷新文档计数（切会话后 chip 上的数字跟着变）
  resetAttachPop();      // 同理：上一个会话的附件浮窗收起并清空
  loadAttachments();     // 刷新附件计数
}

// 关闭 Git 浮窗并清掉上一次的内容：切换任务/工作区后，列表已失效。
// 不保留滚动位置与筛选态——重新打开时重新拉，避免展示别的项目的提交。
function closeGitPop() {
  const pop = $("git-pop");
  if (!pop) return;
  pop.classList.add("hidden");
  $("git-branch-pop").classList.add("hidden");  // 分支面板随之收起
  $("git-body").innerHTML = "";
  $("git-branch").textContent = "";
  $("git-identity").textContent = "";
  $("git-dirty").textContent = "";
  gitOffset = 0;
  gitWho = "all";
}

async function loadHistoryPage() {
  if (!currentSession) return;
  try {
    // 默认最近 100 条；向上翻页带 before_ord（已加载最旧一条的 ord）
    const qs = new URLSearchParams({ limit: "100" });
    if (histOldestOrd != null) qs.set("before_ord", histOldestOrd);
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}/messages?` + qs);
    histHasMore = !!data.has_more;
    // 全量用户提问索引：导航条据此一次性画出整个会话的提问（含尚未加载的）
    if (Array.isArray(data.user_index)) {
      railItems = data.user_index;
      scheduleRail();
    }
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
      // 初始加载的落点：回合进行中定位到最后一条用户消息——那是这个进行中
      // 回合的"阅读起点"，直接贴底看到的却是过程面板里最新的思考流，割裂
      // 且位置随机；回合已结束才贴底（看最终答案）。
      const running = document.querySelector('#chat details.trace.running');
      const lastUser = [...chatEl.querySelectorAll('.bubble.user')].pop();
      const anchor = running && lastUser ? lastUser : null;
      if (anchor) anchor.scrollIntoView({ block: "start" });
      else chatEl.scrollTop = chatEl.scrollHeight;
    }
    updateLoadOlder();
    scheduleRail();  // 历史装载完毕：导航条按新消息重建
  } catch (e) {
    // 历史拉取失败不能静默：以前这里是空 catch，任何渲染/接口异常都被吞掉，
    // 表现成"点进会话一片空白且控制台无痕"，排查代价极高。现在至少留痕 + 给
    // 用户一条可见提示，不再让故障隐形。
    console.error("历史消息加载失败", e);
    const err = document.createElement("div");
    err.className = "bubble error";
    err.textContent = "历史消息加载失败：" + (e && e.message ? e.message : e);
    chatEl.appendChild(err);
  }
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
// ---------- 二期：统一渲染器接线 ----------
// blocks.js / render_blocks.js 在 index.html 里先于 app.js 加载，挂全局。
// 这里把 app.js 现成的零件注入渲染器（不重写、不复制），保证渲染结果与
// 改造前逐字节一致——渲染器只负责「按 Block 结构决定拼装顺序」。
const blocksFromEvents = window.CodingAgentBlocks.blocksFromEvents;
const blocksFromHistory = window.CodingAgentBlocks.blocksFromHistory;
const createLiveTracker = window.CodingAgentBlocks.createLiveTracker;
const blocksRenderer = window.CodingAgentRenderBlocks.createRenderer({
  doc: document,
  buildBubble: buildBubble,
  buildUserBubble: buildUserBubble,
  railTag: railTag,
  makeToolCallLine: makeToolCallLine,
  makeToolResultLine: makeToolResultLine,
  decorateWriteCard: decorateWriteCard,
  metaText: metaText,
  compactCard: compactCard,
  fmtElapsed: fmtElapsed,
  // 历史回放错误卡的"重试上一条"按钮：复用与实时 error 事件相同的
  // retryLast() 路径（与 send() 同一条发送链路）。
  makeRetryButton: () => {
    const btn = document.createElement("button");
    btn.className = "retry-btn";
    btn.textContent = "↻ 重试上一条";
    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "已重发";
      retryLast();
    });
    return btn;
  },
});

// 历史回放：单条存储消息 → DOM 节点。
// 二期起改为「消息 → Block → 统一渲染器」两步走：Block 由 blocks.js 归一化
// （与实时事件流同一套结构），渲染由 render_blocks.js 统一负责——回放与实时
// 不再各写一套，从根上消除"两边不同步"的 bug。
// 仅两处留在本函数：外置归档消息（非 Block 语义）与带附件的用户消息（需先
// 归一化，见下）。其余全部委托。
function historyNode(m) {
  // 外置归档消息：库行内只有 head 预览（超大正文存 artifacts 文件），
  // 展示"内容过大已归档"标记，点开按需拉取全文
  if (m.artifact) return railTag(artifactCard(m), m.mid, "user");
  return blocksRenderer.renderBlocks(blocksFromHistory([m]));
}

// 外置归档消息：超大正文不随时间线整页带回，用户要看时才走 artifact 接口取。
// 带图/带附件文本的用户消息直接按【正常用户气泡】渲染（右侧、图片可点放大）：
// 先用 head 预览画占位，后台取回归档正文后原位替换——用户消息显示成"归档
// 卡片"很反直觉。其余归档（超大工具输出/助手消息）维持"点开加载全文"卡片。
function artifactCard(m) {
  const placeholder = (text) => buildBubble("user", text || "（仅附件）");
  if (m.role === "user") {
    // 从 head 预览里能挤出文字部分（head 是完整消息 JSON 的前 2000 字符）
    let headText = "";
    try { headText = (JSON.parse(m.head).content || [])
      .filter(p => p.type === "text").map(p => p.text || "").join("\n"); } catch { /* head 截断处非法 JSON */ }
    const holder = document.createElement("div");
    // 不占布局（display:contents）：气泡要直接成为 #chat（flex 容器）的子项，
    // align-self 才能让它靠右——包普通 div 会复现"用户消息挤到左侧"的 bug
    holder.style.display = "contents";
    holder.appendChild(placeholder(headText));
    api(`/api/sessions/${encodeURIComponent(currentSession)}` +
        `/artifact?path=${encodeURIComponent(m.path)}`).then((r) => {
      const msg = r.message;
      const parts = Array.isArray(msg.content) ? msg.content : [];
      const text = parts.filter(p => p.type === "text")
        .map(p => p.text || "").join("\n");
      const imgs = parts.filter(p => p.type === "image_url")
        .map(p => ({ kind: "image", name: "", preview: (p.image_url || {}).url || "" }));
      const full = buildUserBubble(text, imgs);  // data URI 进 <img>，可点放大
      holder.replaceChildren(full);              // 原位替换占位气泡
      scrollBottom();                            // 图片加载会撑高；贴底才跟随
    }).catch(() => { /* 取回失败：保留 head 预览占位，不打扰 */ });
    return holder;
  }
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
      if (msg.role === "user" && parts) {  // 翻页等路径下的兜底，同上还原
        const text = parts.filter(p => p.type === "text")
          .map(p => p.text || "").join("\n");
        const imgs = parts.filter(p => p.type === "image_url")
          .map(p => ({ kind: "image", name: "", preview: (p.image_url || {}).url || "" }));
        box.innerHTML = "";
        box.appendChild(buildUserBubble(text, imgs));
      } else {
        pre.textContent = prettyJson(JSON.stringify(msg));
      }
    } catch (e) {
      pre.textContent = "全文读取失败：" + e.message;
    }
  });
  return d;
}

// 空会话占位：居中的浅色提示（不是气泡——没有"AI 先开口"的对话假象）
function welcome() {
  const div = document.createElement("div");
  div.className = "empty-hint";
  div.textContent = "发一条消息开始对话；可附图片或文件，让助手读代码、写代码、跑命令。";
  chatEl.appendChild(div);
}

// ---------- 附件（图片 / 文本文件）----------
// 图片以 base64 作为视觉输入发给模型；文本文件解码后注入上下文。
// 都只存在数据库的消息里，不另外落盘。
let attachments = [];  // {kind: "image"|"text", name, mime, data(base64), preview}

// 附件草稿按会话归属（与文本草稿同一套 key 语义）。存内存不落 localStorage：
// base64 图片几 MB，写进去会撑爆配额且每次切会话都要序列化。会话间存/取由
// saveAttachDraft / restoreAttachDraft 在切会话的同一时点完成。
const attachDrafts = new Map();  // sid(或 __new__) -> attachments 数组快照

function saveAttachDraft(sid) {
  const key = sid || DRAFT_NEW;
  // 只存读好数据的附件：仍在读文件（data 为空）的占位不跨会话保留，
  // 它的 FileReader 回调绑在旧列表上，带过去只会留下永远加载不出的空卡片。
  const ready = attachments.filter(a => a.data);
  if (ready.length) attachDrafts.set(key, ready);
  else attachDrafts.delete(key);  // 空托盘不留残留，避免下次误恢复
}

function restoreAttachDraft(sid) {
  const key = sid || DRAFT_NEW;
  attachments = (attachDrafts.get(key) || []).slice();  // 浅拷贝：数组归属按会话
  renderAttachTray();
}

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
      const img = msgImage(a.preview);  // 与消息里的图片同构：可点放大/复制
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
    x.addEventListener("click", (e) => {
      e.stopPropagation();  // 别让「移除」的点击顺带触发卡片的放大
      attachments.splice(i, 1);
      renderAttachTray();
    });
    card.appendChild(x);
    // 点击卡片任意处也能放大：图片本身很小（64px 卡），只靠 img 的命中区
    // 用户经常点空——点卡片主体同样打开灯箱。删除按钮已在上方 stopPropagation。
    if (a.kind === "image" && a.preview) {
      card.classList.add("clickable");
      card.addEventListener("click", (e) => {
        if (e.target.closest(".att-del")) return;
        openLightbox(a.preview);
      });
    }
    tray.appendChild(card);
  });
}

function addFileToAttachments(file) {
  if (!file) return;
  if (attachments.length >= MAX_ATTACH) { toast(`一次最多 ${MAX_ATTACH} 个附件`); return; }
  const isImage = file.type.startsWith("image/");
  if (isImage && file.size > 4 * 1024 * 1024) { toast(`图片超过 4MB`); return; }
  if (!isImage && file.size > 5 * 1024 * 1024) { toast(`文件超过 5MB（附件限制）`); return; }
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

// ---------- 图片灯箱：点缩略图 → 全屏查看（多图左右切换 + 缩放 + 复制） ----------
// 全局单例：任意消息（实时/历史/归档还原）里的 .msg-img 点击后都进这里。
// openLightbox(src) 单图；openLightbox(src, gallery, idx) 传入同组图片数组
// （整条消息的全部图片）后可左右切换。缩放：+/−/重置按钮、滚轮、键盘。
let lbScale = 1;
let lbKeysHandler = null;  // 当前灯箱的键盘处理器（closeLightbox 要摘掉它）

function openLightbox(src, gallery, idx) {
  closeLightbox();
  lbScale = 1;
  const list = Array.isArray(gallery) && gallery.length ? gallery : [src];
  let cur = Math.max(0, idx || 0);

  const box = document.createElement("div");
  box.className = "lightbox";
  const img = document.createElement("img");
  img.className = "lb-img";

  const apply = () => {
    img.src = list[cur];
    img.style.transform = `scale(${lbScale})`;
  };
  const setScale = (s) => { lbScale = Math.min(5, Math.max(0.2, s)); apply(); };
  const step = (d) => { cur = (cur + d + list.length) % list.length; lbScale = 1; apply(); };

  // 左右切换箭头（多图才显示）。缩放走滚轮，复制走右键菜单——不放工具条
  const mkNav = (label, d) => {
    const b = document.createElement("button");
    b.className = "lb-nav";
    b.textContent = label;
    b.addEventListener("click", (e) => { e.stopPropagation(); step(d); });
    return b;
  };
  const prev = mkNav("‹", -1), next = mkNav("›", 1);

  box.append(prev, img, next);
  // 点放大的图片本身恢复原样；空白处关闭；滚轮缩放
  img.addEventListener("click", () => closeLightbox());
  box.addEventListener("click", (e) => { if (e.target === box) closeLightbox(); });
  box.addEventListener("wheel", (e) => {
    e.preventDefault();
    setScale(lbScale * (e.deltaY < 0 ? 1.12 : 0.89));
  }, { passive: false });
  document.addEventListener("keydown", lightboxKeys);
  lbKeysHandler = lightboxKeys;
  document.body.appendChild(box);
  apply();

  function lightboxKeys(e) {
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === "+" || e.key === "=") setScale(lbScale + 0.25);
    else if (e.key === "-") setScale(lbScale - 0.25);
    else if (e.key === "0") setScale(1);
  }
}
function closeLightbox() {
  document.querySelector(".lightbox")?.remove();
  if (lbKeysHandler) {
    document.removeEventListener("keydown", lbKeysHandler);
    lbKeysHandler = null;
  }
}

// 气泡里的消息图片统一走这里：带点击放大 + 复制。
// 同一条消息的多张图自动编成一组（点击任意一张后可左右切换）。
function msgImage(src, gallery) {
  const img = document.createElement("img");
  img.src = src;
  img.className = "msg-img clickable";
  img.title = "点击放大";
  if (gallery && gallery.length > 1) {
    const idx = Math.max(0, gallery.indexOf(src));
    img.addEventListener("click", () => openLightbox(src, gallery, idx));
  } else {
    img.addEventListener("click", () => openLightbox(src));
  }
  return img;
}

// 注：历史回放的执行过程折叠条（原 traceFromHistory）已由二期统一渲染器接管——
// 见 blocks.js 的 blocksFromHistory + render_blocks.js 的 process 分支。
// 此处不再保留旧实现：同一语义只留一处，避免两边再次不同步。

// 带附件的用户气泡：文字 + 图片缩略图（多图走 2 列网格）/文件名
function buildUserBubble(text, atts) {
  const div = document.createElement("div");
  div.className = "bubble user";
  if (text) {
    const t = document.createElement("div");
    t.className = "ub-text";  // 回退编辑时按类名取原文（不碰附件节点）
    t.textContent = text;
    div.appendChild(t);
  }
  // ✏️ 回退编辑入口（ZCode editUserQuery）：悬停浮现，点击把这一轮退回输入框。
  // mid 在点击时从最近的 [data-role="user"] 上读——自己刚发的气泡要等 turn_end
  // 才回填 mid，构造时不知道，所以不能在渲染期绑定。
  const edit = document.createElement("button");
  edit.className = "ub-edit";
  edit.textContent = "✏️";
  edit.title = "退回到这一轮重新编辑";
  div.appendChild(edit);
  const imgs = (atts || []).filter(a => a.kind === "image" && a.preview);
  const sources = imgs.map(a => a.preview);  // 同组：灯箱左右切换的序列
  for (const a of atts || []) {
    if (a.kind === "image" && a.preview) {
      div.appendChild(msgImage(a.preview, sources));
    } else {
      // 文件名可点：直接打开会话附件浮窗并定位到这份文件——否则附件发出去
      // 之后就只剩这行死文本，用户想再看一眼只能去翻磁盘。
      if (a.kind === "image") {
        // 图片但 preview 为空（turn_start 补发路径只带 kind/name，不落盘所以
        // 没有可回放的预览）：直接跳过不渲染。占位芯片既无信息量又误导
        // （点了没反应），宁缺勿滥——文字正文不受影响。
      } else {
        // 文件名可点：直接打开会话附件浮窗并定位到这份文件——否则附件发出去
        // 之后就只剩这行死文本，用户想再看一眼只能去翻磁盘。
        const f = document.createElement("div");
        f.className = "att-file clickable";
        f.textContent = "📄 " + a.name;
        f.title = "点击查看这份附件";
        f.addEventListener("click", (e) => {
          // stopPropagation 必须加：点击会冒泡到 document 上的「点空白处关闭浮
          // 窗」监听器——它看到目标既不在 attach-pop 内也不在 attach-chip 上，
          // 会把刚打开的浮窗在同一瞬间关掉，表现就是"点了没反应"。
          e.stopPropagation();
          const pop = $("attach-pop");
          if (pop.classList.contains("hidden")) toggleAttachPop();
          openAttachment(a.name);
        });
        div.appendChild(f);
      }
    }
  }
  if (imgs.length > 1) div.classList.add("multi-img");
  return div;
}

function userBubble(text, atts, mid) {
  chatEl.appendChild(railTag(buildUserBubble(text, atts), mid, "user"));
  scrollBottom(true);  // 用户自己的消息永远贴底（同时恢复跟随）
}

// ---------- 回退编辑（ZCode editUserQuery rewind 语义） ----------
// 点用户气泡上的 ✏️：该轮原文回到输入框；再次发送时先调 truncate 端点删掉
// 这一轮及其后的消息，再作为新的一轮发出。已被压缩进摘要的旧消息后端会拒绝
// （摘要引用会悬空），前端原样展示报错。
let editTarget = null;      // {mid}：正在回退编辑的用户消息
let lastTruncateAt = 0;     // 本 tab 刚执行过回退的时间戳：history_truncated 回放去重

function startEdit(bubbleEl) {
  if (streaming) { toast("生成中：请先停止或等回合结束再回退"); return; }
  const mid = bubbleEl && bubbleEl.dataset.mid;
  if (!mid) { toast("这条消息还没有落库，稍等片刻再试"); return; }
  const textEl = bubbleEl.querySelector(".ub-text");
  editTarget = { mid };
  inputEl.value = textEl ? textEl.textContent : "";
  $("edit-banner").classList.remove("hidden");
  inputEl.focus();
}

function cancelEdit() {
  editTarget = null;
  $("edit-banner").classList.add("hidden");
}

// 事件委托：✏️ 按钮在所有用户气泡上动态存在，一个监听器统一接管
chatEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".ub-edit");
  if (!btn) return;
  e.stopPropagation();
  startEdit(btn.closest('[data-role="user"]'));
});

// ---------- 模型：激活切换（工具栏气泡）+ 供应商管理（弹窗） ----------
let providers = [];        // 供应商列表缓存（含各自模型）
let activeModel = { provider_id: "", model: "" };
let activeModelVision = true;  // 激活模型是否支持看图（/api/config 提供）
let editingProvId = null;  // 管理面板当前打开的供应商（null = 新供应商未保存）
let editorModels = [];     // 管理面板里正在编辑的模型行

// 模型是【按会话】存的（sessions.provider_id/model）：没单独选过的会话跟随
// 全局默认（settings.active_model，也就是新任务的初始模型）。sid 为空 = 看全局默认。
async function loadConfig(sid = null) {
  try {
    const q = sid ? `?session_id=${encodeURIComponent(sid)}` : "";
    const cfg = await api("/api/config" + q);
    // 竞态闸门：切会话是异步的，慢响应可能在切走之后才回来——回显的
    // session_id 与请求的不符就丢弃，免得工具栏标签被上一个会话的模型覆盖。
    if ((cfg.session_id || "") !== (sid || "")) return;
    activeModel = { provider_id: cfg.provider_id, model: cfg.model };
    contextWindow = cfg.context_window || contextWindow;
    activeModelVision = !!cfg.vision;   // 激活模型能否看图（决定附件上传时的提示）
    lastCfg = cfg;                      // 设置面板展示用
    const label = `${cfg.provider_name || "模型"} / ${cfg.model || "—"}`;
    $("model-label").textContent = label;
    $("model-chip").title = sid ? `本任务使用的模型：${label}`
                                : `默认模型（新任务的初始模型）：${label}`;
    updateCtxChip();
  } catch (e) {
    $("model-label").textContent = "模型未配置";
  }
}

// 气泡里列模型。带当前任务 id 请求：勾选态与"切到哪个"都按该任务的模型算；
// 新任务（还没有会话）则以全局默认为基准——在气泡里选中它 = 设定新任务的初始模型。
async function loadModelPop() {
  const sid = currentSession || "";
  const data = await api("/api/models" + (sid ? `?session_id=${encodeURIComponent(sid)}` : ""));
  $("model-pop-head").textContent = sid ? "切换此任务使用的模型"
                                        : "设置默认模型（新任务的初始模型）";
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
        const payload = { provider_id: m.provider_id, model: m.model };
        if (sid) payload.session_id = sid;   // 带 id = 只改这个任务；不带 = 改全局默认
        const cfg = await api("/api/active-model", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        activeModel = { provider_id: cfg.provider_id, model: cfg.model };
        contextWindow = cfg.context_window || contextWindow;
        activeModelVision = !!cfg.vision;   // 视觉标记随模型变（影响发图提示）
        lastCfg = cfg;
        $("model-label").textContent = `${cfg.provider_name} / ${cfg.model}`;
        $("model-pop").classList.add("hidden");
        updateCtxChip();
        toast(sid ? `此任务已切换到 ${cfg.provider_name} / ${cfg.model}`
                  : `新任务默认模型：${cfg.provider_name} / ${cfg.model}`);
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
let wsCustom = false;  // 新任务视图 = 是否已预绑项目；会话视图 = 该会话是否绑定项目

async function loadWorkspace() {
  // 新任务视图：不拉用户默认（新任务不预绑任何目录）——显示「选择项目」，
  // 已通过组头「＋」/chip 预选的显示项目名
  if (!currentSession) {
    wsCustom = !!pendingProjectPath;
    setWsLabel(pendingProjectPath || "");
    renderWsChip();
    return;
  }
  try {
    const w = await api("/api/workspace" + `?session_id=${encodeURIComponent(currentSession)}`);
    wsCustom = !!w.custom;
    setWsLabel(w.path);
    renderWsChip();
    refreshGitChip();  // 工作区变了，分支徽章立刻跟着换（不等下一次切会话）
    loadPermMode();  // 工作区变了，权限模式跟着工作区走
  } catch (e) { /* 忽略 */ }
}

function setWsLabel(path) {
  const seg = (path || "").replace(/\/+$/, "").split("/").pop() || path;
  $("ws-short").textContent = seg || "工作区";
  $("ws-pick").title = path || "";
}

function renderWsChip() {
  // 未选过项目：像"选择项目"的下拉（示意待选）；选过：显示目录名
  $("ws-short").textContent = wsCustom ? (($("ws-pick").title || "").replace(/\/+$/, "").split("/").pop() || "工作区")
                                        : "选择项目";
  $("ws-pick").classList.toggle("ws-unset", !wsCustom);
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
    const icon = document.createElement("span");
    icon.className = "m-dir-icon";
    icon.textContent = "📁";
    const label = document.createElement("span");
    label.className = "m-dir-name";
    label.textContent = name;
    const go = document.createElement("span");
    go.className = "m-dir-go";
    go.textContent = "›";
    li.append(icon, label, go);
    li.addEventListener("click", () => navTo((mCwd.endsWith("/") ? mCwd : mCwd + "/") + name));
    ul.appendChild(li);
  }
  if (!info.dirs.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "📂 这里没有子目录了";
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

// 关闭选择弹窗：✕ / 取消 / Esc / 点遮罩空白处都走这里
function closePicker() {
  $("modal").classList.add("hidden");
}

async function chooseWorkspace() {
  const path = mCwd;
  // 新任务还没有会话：选择=预绑到这个任务的首次发送（建会话时原子绑定）
  if (!currentSession) {
    pendingProjectPath = path;
    wsCustom = true;
    setWsLabel(path);
    renderWsChip();
    refreshGitChip();  // 预选项目后徽章立即显示该项目的分支（不必等首条消息）
    $("modal").classList.add("hidden");
    toast(`已选择项目 ${path.split("/").pop()}，发送消息时绑定`);
    return;
  }
  try {
    const w = await api("/api/workspace", {
      method: "POST", body: JSON.stringify({ path, session_id: currentSession }),
    });
    wsCustom = true;
    setWsLabel(w.path);
    renderWsChip();
    $("modal").classList.add("hidden");
    bubble("note", `（本任务已绑定项目 ${w.path}，之后我的文件操作和命令都在这个目录里进行；其他任务不受影响）`);
  } catch (e) {
    $("m-path").textContent = "切换失败：" + e.message;
  }
}

// ---------- 权限模式（按工作区记忆；闸门每次判定现读，切换立即生效） ----------
const PERM_LABELS = { readonly: "只读", confirm: "确认", yolo: "完全访问" };
let permMode = "confirm";

async function loadPermMode() {
  if (!currentSession) return;  // 新任务没有会话级模式，保持全局选择
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(currentSession)}/perm_mode`);
    permMode = r.mode || "confirm";
    renderPermChip();
  } catch (e) { /* 拉取失败保留当前显示 */ }
}

function renderPermChip() {
  $("perm-label").textContent = PERM_LABELS[permMode] || permMode;
  $("perm-chip").classList.toggle("perm-chip-warn", permMode === "yolo");
}

function togglePermPop() {
  const pop = $("perm-pop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  for (const row of pop.querySelectorAll(".perm-row")) {
    row.classList.toggle("active", row.dataset.mode === permMode);
    row.onclick = async () => {
      const mode = row.dataset.mode;
      pop.classList.add("hidden");
      if (!currentSession) {  // 新任务：先记住选择，发送建会话后跟随工作区写入
        permMode = mode; renderPermChip(); return;
      }
      try {
        const r = await api(`/api/sessions/${encodeURIComponent(currentSession)}/perm_mode`,
          { method: "POST", body: JSON.stringify({ mode }) });
        permMode = r.mode; renderPermChip();
        toast(`权限模式：${PERM_LABELS[permMode]}`);
      } catch (e) { toast("切换失败：" + e.message); }
    };
  }
  const rect = $("perm-chip").getBoundingClientRect();
  pop.style.left = Math.max(8, rect.left) + "px";
  pop.style.bottom = (innerHeight - rect.top + 8) + "px";
  pop.classList.remove("hidden");
}

// ---------- 上下文容量 ----------
function updateCtxChip() {
  const chip = $("ctx-chip");
  if (!usageNow) { chip.textContent = "⛁ —"; return; }
  // context_tokens = 最近一次真实请求的 prompt_tokens（模型当前上下文大小）；
  // prompt_tokens 是回合内多轮请求的累加值（历史被重复计数），只作兼容回退。
  chip.textContent = `⛁ ${fmtWan(usageNow.context_tokens ?? usageNow.prompt_tokens)} / ${fmtWan(contextWindow)}`;
}

async function refreshCtx() {
  if (!currentSession) { usageNow = null; updateCtxChip(); return; }
  try {
    const c = await api(`/api/context?session_id=${encodeURIComponent(currentSession)}`);
    contextWindow = c.window || contextWindow;
    usageNow = { prompt_tokens: c.tokens, context_tokens: c.tokens,
                 context: c.breakdown, cache_hit_rate: c.cache_hit_rate };
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

// 分段条配色：与 ctx-breakdown 行前的色点一一对应（常量表，不涉不可信内容）
const CTX_SEG_COLORS = {
  system: "#8b949e", tools: "#d29922", user: "#3fb950",
  assistant: "#58a6ff", tool_results: "#bc8cff",
};

function renderCtxPop() {
  const nums = $("ctx-nums"), fill = $("ctx-fill"), bd = $("ctx-breakdown");
  const tokens = usageNow ? (usageNow.context_tokens ?? usageNow.prompt_tokens) : 0;
  const pct = Math.min(100, (tokens / contextWindow) * 100);
  nums.textContent = `${fmtWan(tokens)} / ${fmtWan(contextWindow)}（${pct.toFixed(1)}%）`;
  fill.style.width = pct + "%";
  fill.classList.toggle("warn", pct > 80);

  // 分段条：进度条内部按五类构成切分（ZCode contextUsage 的 breakdown
  // 可视化）——一眼看到 token 花在哪儿，而不是只有一个总数。
  fill.innerHTML = "";  // 只拼固定 key 的 span 常量，无不可信内容
  const b = (usageNow && usageNow.context) || {};
  const bTotal = Math.max(1, Object.values(b).reduce((x, y) => x + (y || 0), 0));
  for (const [key, color] of Object.entries(CTX_SEG_COLORS)) {
    const seg = document.createElement("div");
    seg.className = "ctx-seg";
    seg.style.background = color;
    seg.style.flexGrow = String(b[key] || 0);
    fill.appendChild(seg);
  }

  const labels = { system: "系统提示词", tools: "工具定义", user: "用户消息", assistant: "助手回复", tool_results: "工具结果" };
  bd.innerHTML = "";
  for (const [key, label] of Object.entries(labels)) {
    const row = document.createElement("div");
    row.className = "ctx-row";
    const dot = document.createElement("i");
    dot.className = "ctx-dot";
    dot.style.background = CTX_SEG_COLORS[key];
    const name = document.createElement("span");
    name.textContent = label;
    const val = document.createElement("b");
    val.textContent = ((b[key] || 0) / bTotal * 100).toFixed(1) + "%";
    row.append(dot, name, val);
    bd.appendChild(row);
  }
  // 缓存命中率只在 >78% 时显示（ZCode 同规则）：低命中率展示出来只会
  // 分散注意力——它要么还没稳定（前几轮），要么说明这个供应商不缓存。
  const hit = usageNow && usageNow.cache_hit_rate;
  $("ctx-cache").textContent = hit != null && hit > 78 ? hit + "%" : "—";
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
  // 服务端说回合还在跑：把它的真起点交给本 tab 当计时基准。切会话/刷新回来的
  // 页面没有本地 qStart，不这么做摘要行就会从 0 重新数（秒数跳回 0.x 的由来）。
  // 本 tab 自己发的回合 qStart 更早、更准，保留它（只在不早于服务端起点时覆盖，
  // 避免服务端起点更早导致已显示的秒数倒退）。
  const srvStart = Number(caughtUp && caughtUp.started_at) * 1000;
  if (srvStart > 0 && (!qStart || srvStart > qStart)) qStart = srvStart;
  // 服务端说回合仍在跑：强制进入"生成中"。不能依赖补发段里的 turn_start 来
  // 置位——超长回合（事件量 > 环形缓冲 500）会把 turn_start 挤出缓冲，
  // replay_plan 退化为"尽力补尾巴"（events.py），补发段没有 turn_start，
  // streaming 就永远丢了：按钮停在"发送"态，点击变成再发一条消息进队列，
  // 而不是停止。running===true 时提前置位是幂等的（turn_start 正常到达时
  // 再置一次无副作用），只覆盖"丢了 turn_start"的退化路径。
  if (caughtUp && caughtUp.running === true) setStreaming(true);
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
    for (const { seq: s, evt } of seg.events) {
      // 与实时路径同一条 seq 闸门：补发段会故意从 turn_start 起整段重发
      // 与本页已渲染部分重叠的事件，不跳过的话 turn_start 被重复应用——
      // traceEl/thinkEl 被强制清空重建（秒数闪跳回 0、思考流凭空消失）。
      if (s != null && lastSeq != null && s <= lastSeq) continue;
      applyEvent(evt, s);
    }
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
let traceEl = null;
let traceCurrent = "";  // 当前正在执行的工具（收起状态下摘要行显示的"此刻在干嘛"）
let tracePhase = "";    // 当前阶段文案（"正在理解问题…/深度思考中…/正在撰写回答…"），
                        // 随事件切换、由 traceTick 拼进摘要行；工具执行期间被
                        // traceCurrent 覆盖（"正在读文件…"比笼统的阶段更具体）。
let traceLast = "";     // 摘要行"此刻状态"的实际渲染值 + 变更时刻：极快的事件序列
                        // （git 等秒回命令）下"思考中→命令→思考中"每秒切换好几次，
                        // 视觉上就是闪烁；每个状态至少停留 MIN 状态才被下一个取代
                        // （但正在执行的工具永远优先，保证"正在跑什么"实时可信）。
let traceLastAt = 0;
const TRACE_MIN_STATE_MS = 2000;
let pendingCalls = [];  // 已发出但未见结果的工具调用（算持续时长用）
let permissionCards = new Map();  // permission id -> 卡片元素：补发重放同一请求时复用/整卡重画，不叠卡片
let liveMsgs = new Map();  // mid -> {el, text}：事件流里同一 mid 的 delta 归并进同一气泡
let curMid = null;         // 当前回答段落的 mid（round 事件切换）
// 实时工具记账：与历史回放共用 blocks.js 的配对/状态逻辑（见 createLiveTracker），
// 避免"哪些工具在跑、配到哪个结果"两路径各写一份而不同步。
let liveTracker = createLiveTracker();
let historyMids = new Set();  // 已从分页接口加载进时间线的消息 mid（补发去重基准）

const TOOL_ICONS = {
  write_file: "✏️", apply_patch: "✏️",
  read_file: "🔍", grep: "🔍", list_dir: "📂",
  run_bash: "▶️", calculator: "🧮", current_time: "🕐", get_weather: "🌤️",
  browser_open: "🌐", browser_click: "🖱️", browser_type: "⌨️", browser_screenshot: "📷",
};

function ensureTrace() {
  if (traceEl) return;
  traceEl = document.createElement("details");
  traceEl.className = "trace running";  // running：进行中回合标记（切会话定位锚点用）
  // 默认【收起】：执行过程不是回答。之前生成期间强制展开，几十行浅灰小字
  // 在正文下方滚动，把真正的答案挤出视口——"看不到重点"的直接来源。
  // 改成收起后，摘要行持续显示"当前正在做什么"，既有动静又不抢正文。
  traceEl.open = false;
  const summary = document.createElement("summary");
  summary.textContent = "已思考 0 秒";
  traceEl.appendChild(summary);
  chatEl.appendChild(traceEl);
}

// 生成中的折叠条文案：有工具步显示"已工作"，纯思考阶段（reasoning 流不算步）
// 显示"已思考"。步数取自 liveTracker.state().steps（见 traceTick）。
function fmtElapsed(sec) {
  const n = Number(sec) || 0;
  return n < 60 ? `${n.toFixed(n < 10 ? 1 : 0)} 秒`
    : `${Math.floor(n / 60)} 分 ${Math.round(n % 60)} 秒`;
}

// 回合计时起点（毫秒时间戳）。优先本 tab 自己的 qStart；补发/刷新/切会话
// 路径上没有它（qStart 为 0），退回服务端给的回合起点 startedAt——这样切回来
// 时秒数接着数，而不是从 0 重新爬。
function traceStart() {
  const st = liveTracker.state();
  return qStart || (st && st.startedAt) || 0;
}

// 从 turn_start 事件取回合起点（毫秒）。服务端 started_at 是秒（浮点）。
// 没给（本 tab 自己发消息的正常路径）就取当下。
function turnStartFromEvent(evt) {
  const s = evt && Number(evt.started_at);
  return s > 0 ? s * 1000 : Date.now();
}

// 摘要行 = 一行"当前状态"：正在跑的工具 + 已工作多久 + 步数。
// 这是收起状态下用户唯一能看到的过程信息，必须把"此刻在干嘛"说清楚。
// 步数与"正在跑什么"都取自 liveTracker（与回放同一套记账），不再另立计数器。
function traceTick() {
  if (!traceEl) return;
  const st = liveTracker.state();
  const steps = st ? st.steps : 0;
  const el = fmtElapsed((Date.now() - traceStart()) / 1000);
  // traceCurrent 是本 tab 自己发工具时设的即时值；补发/刷新场景下为空，
  // 此时从 tracker 记账里现取「正在跑的工具」——两处同源，不会各说各话。
  const cur = traceCurrent || traceCurrentFromTracker();
  // 目标状态优先级：正在执行的工具 > 阶段文案（toolResultLine 设置的"✓ 完成"
  // 定格态写入 traceLast，由下面节流延续）> 无。
  // 防闪：刚渲染的状态不满 TRACE_MIN_STATE_MS 不被下一个取代——git 等秒回命令
  // 下"深度思考中→命令→深度思考中"每秒切好几次，视觉即闪烁。例外：正在执行
  // 的工具（cur 非空）直接切换，"正在跑什么"必须实时可信。
  let doing = cur || tracePhase;
  if (doing && traceLast && doing !== traceLast && !cur &&
      Date.now() - traceLastAt < TRACE_MIN_STATE_MS) {
    doing = traceLast;  // 上一状态（含"✓ 完成"定格）停留不满 2s：延续它，不闪
  } else if (doing !== traceLast) {
    traceLast = doing; traceLastAt = Date.now();
  }
  const prefix = doing ? `${doing} · ` : "";
  const label = steps > 0 ? "已工作" : "已思考";
  traceEl.querySelector("summary").textContent =
    `${prefix}${label} ${el} · ${steps} 步`;
}

// 追加一行执行痕迹。步数已由 liveTracker 记账（tool_call 时 +1），这里不再自增。
function appendTrace(el) {
  ensureTrace();
  traceEl.appendChild(el);
  traceTick();
  scrollBottom();  // 执行步骤追加：只在用户本来贴底时跟随
}

function traceLine(text) {
  const div = document.createElement("div");
  div.className = "trace-line";
  div.textContent = text;
  appendTrace(div);
}

const TOOL_KIND = {
  write_file: "写入", apply_patch: "修改",
  read_file: "读取", grep: "搜索", list_dir: "列目录",
  run_bash: "命令", calculator: "计算", current_time: "时间", get_weather: "天气",
  browser_open: "打开", browser_click: "点击", browser_type: "输入", browser_screenshot: "截图",
};

// 构建工具调用行（游离节点）：实时流与历史回放共用
function makeToolCallLine(name, argsStr) {
  let a = {};
  try { a = JSON.parse(argsStr); } catch { /* 参数不是 JSON */ }
  const main = summarize(a.command || a.path || a.expression || a.pattern || a.city || "", 46);
  const kind = TOOL_KIND[name] || "";
  // 写入类（write_file/apply_patch）用卡片：文件名作标题，一眼看到"动了哪个文件"。
  // 其余工具仍是单行摘要——读取/搜索是噪声，不该和写入抢注意力。
  const isWrite = name === "write_file" || name === "apply_patch";
  const d = document.createElement("details");
  d.className = "tl" + (isWrite ? " write card" : "");
  const summary = document.createElement("summary");
  summary.textContent = `${TOOL_ICONS[name] || "🔧"} ${kind}${kind ? " " : ""}${main}`;
  d.append(summary, buildArgsBody(name, a, argsStr));
  return d;
}

// 调用卡的展开正文。apply_patch 渲染成红（删除）/绿（新增）的 diff——直接
// 展示原始参数 JSON 的话，用户看到的是转义成一行的 \n 字符串，完全看不出
// 改了什么（这正是"展开也不清楚"的根源）。write_file 没有旧文可对比，按
// 新增（全绿）展示。其余工具照旧回退到格式化 JSON。
function buildArgsBody(name, a, argsStr) {
  if (name === "apply_patch" && typeof a.search === "string" && typeof a.replace === "string") {
    return buildDiffBody(a.search, a.replace);
  }
  if (name === "write_file" && typeof a.content === "string") {
    return buildDiffBody("", a.content);
  }
  const pre = document.createElement("pre");
  pre.textContent = prettyJson(argsStr);
  return pre;
}

// 逐行 diff 视图：删除行红底、新增行绿底，行首 −/+ 前缀。
// 不做 LCS 精细对齐——apply_patch 的语义本就是"整段 search 换成整段 replace"，
// 按块展示（先整块红、再整块绿）比逐行交错更贴合它的实际行为，也更好读。
function buildDiffBody(search, replace) {
  const wrap = document.createElement("div");
  wrap.className = "diff";
  const push = (text, cls, sign) => {
    if (!text) return;
    // 末尾换行会产生一个多余空行，去掉（不代表真实内容）
    const lines = text.replace(/\n$/, "").split("\n");
    for (const ln of lines) {
      const row = document.createElement("div");
      row.className = "diff-line " + cls;
      const mark = document.createElement("span");
      mark.className = "diff-sign";
      mark.textContent = sign;
      const body = document.createElement("span");
      body.className = "diff-text";
      body.textContent = ln || " ";
      row.append(mark, body);
      wrap.appendChild(row);
    }
  };
  push(search, "del", "−");
  push(replace, "add", "+");
  if (!wrap.childElementCount) {
    const empty = document.createElement("div");
    empty.className = "diff-line";
    empty.textContent = "（无内容）";
    wrap.appendChild(empty);
  }
  return wrap;
}

// 从工具参数里取一个适合放进摘要行的短标签（"正在 run_bash xxx"）
function toolDoingLabel(name, argsStr) {
  let a = {};
  try { a = JSON.parse(argsStr); } catch { /* 非 JSON */ }
  const target = summarize(a.command || a.path || a.pattern || a.expression || a.city || "", 30);
  return `${TOOL_ICONS[name] || "🔧"} ${TOOL_KIND[name] || name}${target ? " " + target : ""}`;
}

function toolCallLine(name, argsStr) {
  const d = makeToolCallLine(name, argsStr);
  appendTrace(d);
  pendingCalls.push({ name, el: d, t: Date.now() });
  traceCurrent = toolDoingLabel(name, argsStr);  // 摘要行显示"此刻在跑什么"
  traceTick();
}

// 从 liveTracker 当前记账里取「正在跑的工具」标签（摘要行用）。
// 与 toolCallLine 里设的值同源，但补发/刷新场景下 tracker 是权威。
function traceCurrentFromTracker() {
  const st = liveTracker.state();
  if (!st) return "";
  for (let i = st.items.length - 1; i >= 0; i--) {
    const it = st.items[i];
    if (it.kind === "tool" && it.status === "running") {
      return toolDoingLabel(it.name, it.arguments || "{}");
    }
  }
  return "";
}

// 构建工具结果行（游离节点）。dur：实时流配对调用算出的耗时；回放没有，传空。
function makeToolResultLine(name, resultStr, dur = "") {
  const d = document.createElement("details");
  d.className = "tl result";
  const summary = document.createElement("summary");
  const pre = document.createElement("pre");
  let parsed = null;
  try { parsed = JSON.parse(resultStr); } catch { /* 纯文本 */ }

  const hintSuffix = (p) => p.hint ? `\n💡 ${p.hint}` : "";

  if (parsed && typeof parsed === "object" && "exit_code" in parsed) {
    // run_bash：非零退出也带完整输出（统一信封下 ok:false 但输出是第一手材料）。
    // 输出只在 result 字段（stdout 与 [stderr] 段已拼合，历史里只存这一份）
    summary.textContent = `↩ ${dur.replace(" · ", "") || "0s"} · exit ${parsed.exit_code}`;
    if (parsed.exit_code !== 0) summary.classList.add("err");
    pre.textContent = parsed.result || "(无输出)";
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
  return d;
}

// tool 块（可选）：来自共用追踪器的配对结果，携带权威的工具状态。
// 计时仍由 DOM 侧的 pendingCalls 负责（那是渲染关注点，与语义无关）。
function toolResultLine(name, resultStr, tool) {
  // 持续时长：配对最近一次同名调用
  let dur = "";
  const idx = pendingCalls.map(c => c.name).lastIndexOf(name);
  if (idx >= 0) {
    const call = pendingCalls.splice(idx, 1)[0];
    dur = ` · ${((Date.now() - call.t) / 1000).toFixed(1)}s`;
    // 写入类：把结果里的增删行数回填成调用卡上的徽章，让"改了多大"一眼可见
    if (call.el && call.el.classList.contains("card")) decorateWriteCard(call.el, resultStr);
  }
  const line = makeToolResultLine(name, resultStr, dur);
  // 追踪器判定的状态落成 DOM 标记：denied（权限拒绝）与 err（失败）都标 err 样式，
  // 与历史回放同一判据（blocks.js 的 toolStatus）——不再各判一次。
  if (tool && (tool.status === "err" || tool.status === "denied")) {
    const sum = line.querySelector("summary");
    if (sum) sum.classList.add("err");
  }
  appendTrace(line);
  // 工具已返回：摘要行从"▶️ 正在命令 xxx"切换为"✓ 命令 xxx · 完成(0.4s)"并
  // 定格到下一次事件——而不是清空退回阶段文案。否则秒回的命令会让"正在→思考
  // 中→正在"来回横跳（闪烁来源）；"call+result 合并为一个连续状态"也符合直觉。
  traceLast = `✓ ${TOOL_KIND[name] || name}${dur ? " " + dur.replace(" · ", "") : ""}`;
  traceLastAt = Date.now();
  traceCurrent = "";
  traceTick();
}

// 在写入卡片的 summary 上追加 +N −M / 新建 N 行 徽章（历史回放没有配对，不调用）
function decorateWriteCard(callEl, resultStr) {
  let p = null;
  try { p = JSON.parse(resultStr); } catch { return; }
  if (!p || typeof p !== "object") return;
  let text = "";
  if ("added" in p) text = `+${p.added} −${p.removed}`;
  else if ("lines" in p) text = `新建 ${p.lines} 行`;
  if (!text) return;
  const s = callEl.querySelector("summary");
  if (!s || s.querySelector(".tl-badge")) return;
  const b = document.createElement("span");
  b.className = "tl-badge";
  b.textContent = text;
  s.appendChild(b);
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
  demoteLiveBubbleToTrace();  // 权限卡前的正文也是过程说明，降级进面板
  ensureTrace();
  let card = permissionCards.get(evt.id);
  if (card) card.remove();  // 同一请求重放：整卡重画，绝不允许出现两张活卡
  card = document.createElement("div");
  card.className = "perm-card";
  notifyDesktop("需要确认权限", `${evt.tool}：${summarize(evt.reason || "", 60)}`);
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
  scrollBottom(true);  // 确认卡是必须看到的交互：强制贴底
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
    // 流式期间正文一律按【过程样式】渲染（小字，挂在执行过程面板内）：此刻
    // 还无法预知这段是过程说明还是最终答案——要等 done 才知道。先小字流出，
    // 若最终是答案，done 时把它移出面板、升级为正常正文卡（见 finalizeAnswer）。
    // 这样避免了"先大字流出、再缩成小字"的跳动（旧实现每段都缩一次）。
    ensureTrace();
    // 展开面板：正文流式输出必须让用户看得见"它在动"——收起状态下过程文字
    // 不可见，用户会以为卡死。done 时统一收起（答案回归正文区）。
    traceEl.open = true;
    const el = document.createElement("div");
    el.className = "process-text streaming";
    traceEl.appendChild(el);
    b = { el, text: "" };
    liveMsgs.set(mid, b);
    if (!metaEl) {
      metaEl = document.createElement("div");
      metaEl.className = "meta";
    }
    metaEl.textContent = metaText(((Date.now() - traceStart()) / 1000).toFixed(1), usageNow);
    chatEl.appendChild(metaEl);  // 统计行留在正文区末尾，跟随最终答案
  }
  liveBubble = b.el;  // 兼容既有的"当前气泡"语义（retire/done 收尾用）
  scrollBottom();
  return b;
}

// SSE 事件 → 页面更新（事件类型见 backend/app.py 的 _run_round）
function retireLiveBubble() {
  // 当前气泡"退役"：去掉打字机光标（否则中间轮次的气泡会一直闪），空的直接移除
  if (!liveBubble) return;
  liveBubble.classList.remove("streaming");
  if (!liveBubble.textContent) liveBubble.remove();
}

// 把当前流式正文"定格为过程说明"：下一个 round / tool_call / 权限卡事件一到，
// 就证明这段不是最终答案——它本来就在执行过程面板里（ensureLiveMsg 挂进去的），
// 这里只需去掉打字机光标、清掉空块。文字为空（模型只调工具没输出正文）则移除。
// 最终答案走的是相反方向：finalizeAnswer 把它移出面板、升级为正文卡。
function demoteLiveBubbleToTrace() {
  const el = liveBubble;
  if (!el) return;
  el.classList.remove("streaming");
  if (!el.textContent.trim()) { el.remove(); return; }
  // 确认是过程说明：补上「💬 说明」标记（.demoted），与上方思考流区分开。
  // 与历史回放（blocks.js 的 process_text → note.demoted）保持同一副面孔。
  el.classList.add("demoted");
}

// 最终答案定稿：把流式期间挂在执行过程面板里的那个气泡【移出面板】，插到
// 正文区（折叠条之后）、升级为正常字号，并做 Markdown 渲染。
// 这是"先小字流出、完成后升级"的落点——升级只发生一次，且是"小→大"的揭晓，
// 不像旧实现每段都"大→小"地缩一次。
function finalizeAnswer(el, text, mid) {
  el.classList.remove("streaming", "process-text", "demoted");  // 去掉过程小字样式与「💬 说明」标记，换成正文卡
  el.classList.add("bubble", "assistant");
  traceEl.after(el);  // 紧跟折叠条：答案在执行过程之后，符合阅读顺序
  renderIntoBubble(el, text);
  railTag(el, mid, "assistant");  // 仍打锚（保留 mid 标识），但导航条只画用户提问
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
    scrollBottom();  // 流式增量：只在用户贴底时跟随，上滑看历史时不打扰
  }
  if (pendingThink) {
    if (thinkEl) {
      thinkEl.textContent += pendingThink;
      thinkEl.scrollTop = thinkEl.scrollHeight;
    }
    pendingThink = "";
    scrollBottom();
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
    // 原文补画（附件只带名字：图片 base64 不该进环形缓冲占容量）。
    // 另一种不补画的情况：输入消息已随回合开始提前落库、且刚加载的历史里
    // 已有它（user_mid 在 historyMids）——切会话回来的页面时间线已含这条
    // 输入，再画一次就重复了。
    const alreadyShown = evt.user_mid && historyMids.has(evt.user_mid);
    if ((!evt.nonce || evt.nonce !== myNonce) && !alreadyShown) {
      userBubble(evt.input || "（仅附件）",
        (evt.atts || []).map(a => ({ kind: a.kind, name: a.name, preview: "" })),
        evt.user_mid);
      scheduleRail();  // 补画的用户消息也要进导航条
    }
    // 回合级状态复位（原在 performSend 里；改为事件驱动后，刷新页面接上
    // 正在进行的回合也走同一套初始化）
    liveMsgs = new Map();
    liveBubble = null; traceEl = null; traceCurrent = "";
    tracePhase = "正在理解问题…";  // 回合开场：模型还没吐任何内容时的友好占位
    metaEl = null; thinkEl = null; pendingCalls = [];
    liveTracker.reset();  // 工具记账随回合重置（与 blocksFromEvents 的 turn_start 行为一致）
    // 服务端回合起点喂给记账器：补发/切会话场景下 qStart 为 0，traceTick 的
    // traceStart() 退回 tracker.startedAt——没有这条，秒数基准会漂移闪跳。
    liveTracker.feed({ type: "turn_start", started_at: evt.started_at });
    permissionCards = new Map();  // 新回合的确认卡是新的请求：旧卡引用随时间线一起失效
    pendingDeltas = new Map(); pendingThink = "";
    usageNow = null; curMid = null;
    // 计时起点：本 tab 自己发消息时不存在 evt.started_at（该字段是服务端给
    // 补发/多标签页路径用的），取当下；有则用服务端给的回合真起点。
    qStart = turnStartFromEvent(evt);
    clearInterval(metaTimer);
    metaTimer = setInterval(() => {
      if (metaEl && streaming) metaEl.textContent = metaText(((Date.now() - traceStart()) / 1000).toFixed(1), usageNow);
      if (traceEl && streaming) traceTick();  // 折叠条秒数实时跳动
    }, 100);
    setStreaming(true);
    loadSessions();  // 新任务/新标题此刻才在服务端落定，列表刷新
  } else if (t === "round") {
    flushStreamBuffers();  // 上一轮的增量先落进旧气泡，再开新一轮
    // 上一段正文（若有）是"过程性说明"：新轮次已开，证明它不是最终答案。
    // 先降级进执行过程面板（顺序在轮次标题之前，读起来才顺），再写标题。
    demoteLiveBubbleToTrace();
    traceLine(`🧠 思考 · 第 ${evt.round} 轮`);
    tracePhase = "深度思考中…";  // 推理/正文还没来，先给个阶段占位
    thinkEl = null;  // 新一轮的思考流开一个新块
    curMid = evt.mid;  // 本轮回答段落的 mid：后续 delta/done 归并的键
  } else if (t === "reasoning_delta") {
    // 思考模型的推理过程实时流进「执行过程」面板当前轮次下方：
    // 思考阶段再长界面也有动静，不会再像假死；面板收起后不占聊天区。
    // 推理与回答是两个流：这里绝不带 mid（后端也不再发），否则同一 mid 会
    // 把推理归并进回答气泡——思考过程冒充正文正是要杜绝的那个 bug。
    if (!thinkEl) {
      ensureTrace();
      // 本轮已经有过工具调用（在收起面板里跑完了一堆步骤）——说明用户是在
      // 生成中途切回/刷新回来的，此刻补发的推理流属于「过去」。这时不再强制
      // 展开面板：否则切会话的瞬间会看到过程面板"啪"地弹开、正文区跟着跳一下。
      // 当前正在产出的推理会实时填进去，用户点开折叠条一样能看到。
      if (!liveTracker.state()?.steps) traceEl.open = true;
      thinkEl = document.createElement("div");
      thinkEl.className = "think-line";
      traceEl.appendChild(thinkEl);  // 不走 appendTrace：思考流不算一步
    }
    queueStreamDelta("think", null, evt.delta);
  } else if (t === "answer_delta") {
    tracePhase = "正在撰写回答…";
    // 非思考模型没有 reasoning_delta，正文流就是它"思考过程"的唯一可见形态
    // （多轮工具回合尤其如此）——与 reasoning_delta 同样展开面板，避免收起
    // 状态下过程静默累积、用户只见秒数跳动。
    if (!liveTracker.state()?.steps) traceEl.open = true;
    queueStreamDelta("answer", evt.mid, evt.delta);
  } else if (t === "todo_update") {
    // 任务清单卡：实时路径也画在聊天区（最新一份替换旧的，与 blocks.js 的
    // 去重规则一致）。回放路径由 blocksFromHistory→renderBlocks 走 todo 块。
    const todos = Array.isArray(evt.todos) ? evt.todos : [];
    if (todos.length) {
      const frag = blocksRenderer.renderBlocks([{ kind: "todo", todos }]);
      const old = chatEl.querySelector(":scope > .todo-card");
      if (old) old.replaceWith(frag); else chatEl.appendChild(frag);
      scrollBottom();
    }
  } else if (t === "tool_call") {
    flushStreamBuffers();
    // 调工具前输出的正文同样是过程说明（"我先看看这个文件…"），一并降级
    demoteLiveBubbleToTrace();
    tracePhase = "";  // 阶段让位：接下来摘要行显示具体的工具名（"正在读取…"）
    // 记账交给共用追踪器（与回放同一套配对逻辑），DOM 侧只负责画这一行
    const act = liveTracker.feed(evt);
    if (act && act.kind === "tool_open") toolCallLine(act.tool.name, act.tool.arguments);
    else toolCallLine(evt.name, evt.arguments);  // 兜底：极端序列下仍画出来
  } else if (t === "tool_result") {
    const act = liveTracker.feed(evt);
    toolResultLine(evt.name, evt.result, act && act.kind === "tool_close" ? act.tool : null);
  } else if (t === "permission_request") {
    liveTracker.feed(evt);  // 记账（tool_wait），DOM 由 showPermissionCard 画
    showPermissionCard(evt);
  } else if (t === "usage") {
    usageNow = evt;   // 供上下文气泡与统计行使用
    updateCtxChip();
    if (metaEl) metaEl.textContent = metaText(evt.elapsed_s, usageNow);
  } else if (t === "done") {
    pendingDeltas = new Map(); pendingThink = "";  // 完整回答直接覆盖，丢弃未刷的增量，防止 rAF 晚到追加旧文本
    const b = ensureLiveMsg(evt.mid);
    b.el.classList.remove("streaming");
    // done.answer 是权威正文，但只在它"确有内容"时才覆盖气泡：
    // 为空（模型本轮只产出推理）或与本轮已收到的增量不符时整体覆盖，会把
    // 推理文字或空白写进正文区——思考过程属于「执行过程」面板，不是回答。
    const authoritative = typeof evt.answer === "string" ? evt.answer : "";
    // 定稿即把纯文本气泡升级为 Markdown 渲染：流式期间用 textContent 快刷，
    // 只有此刻（内容已冻结）才做一次解析重建——既保住打字机性能，又让最终
    // 答案带上标题/列表/代码块的骨架。下面三处赋值统一走 renderIntoBubble。
    if (!authoritative) {
      // 正文为空：保留已流出的增量（若有），一条都没有则不留空白气泡
      b.text = b.text || "";
      if (!b.text) b.el.remove();
      else finalizeAnswer(b.el, b.text, evt.mid);
    } else if (authoritative === b.text) {
      finalizeAnswer(b.el, b.text, evt.mid);  // 与增量一致：照常定稿
    } else if (b.text && !authoritative.includes(b.text)) {
      // 服务端正文与已渲染增量对不上（疑似推理混入/乱序）：保留用户已看到的
      // 流式内容，不整体覆盖，并留一行提示便于排查
      finalizeAnswer(b.el, b.text, evt.mid);
      const warn = document.createElement("div");
      warn.className = "meta";
      warn.textContent = "（本轮回答与流式内容不一致，已保留流式版本）";
      chatEl.appendChild(warn);
    } else {
      b.text = authoritative;
      finalizeAnswer(b.el, authoritative, evt.mid);
    }
    clearInterval(metaTimer);
    metaEl.textContent = metaText(evt.elapsed_s, evt.usage);
    if (evt.mid) historyMids.add(evt.mid);  // 已在屏上：防后续补发重复渲染
    if (traceEl) {
      // 做完任务自动折叠：正文回归"只要答案"；点折叠条仍可回看全过程
      traceEl.open = false;
      const st = liveTracker.state();
      traceEl.querySelector("summary").textContent =
        `已工作 ${fmtElapsed(evt.elapsed_s)} · ${st ? st.steps : 0} 步`;
    }
    scrollBottom();  // 回答完成：贴底用户直接看到答案，上滑用户不受打扰
    loadSessions();  // 任务时间/排序刷新
  } else if (t === "compacted") {
    // 回答结束后的自动压缩（不产生回答流）：补一张分隔卡片并刷新容量显示。
    // 到达顺序在 done 之后——回答气泡已定稿，卡片插在对话流末尾即正确位置。
    chatEl.appendChild(compactCard(evt.summary));
    scrollBottom();
    usageNow = { prompt_tokens: evt.prompt_tokens, context_tokens: evt.prompt_tokens,
                 context: evt.context, cache_hit_rate: null };
    updateCtxChip();
    toast("早期对话已压缩为摘要，上下文占用已下降");
  } else if (t === "turn_end") {
    // 回合结束（服务端已落盘）：驱动排队队列推进的唯一信号——原来靠 POST
    // 收尾推进，现在 POST 立即返回，队列只能跟着回合生命周期走
    if (evt.user_mid) historyMids.add(evt.user_mid);
    // 本 tab 自己发消息时画的气泡当时还没有 mid（服务端此刻才落定）：回填锚点，
    // 否则这条用户消息在导航条里永远缺席（其他标签页/刷新路径在 turn_start 已带上）
    if (evt.user_mid) tagLastUntaggedUser(evt.user_mid);
    refreshUserIndex();  // 本轮提问此刻才落库：导航条补上这一条
    clearInterval(metaTimer);
    setStreaming(false);
    myNonce = null;
    // 本会话就在前台跑完：用户亲眼看到了结果，立即标记已读——否则服务端刚置的
    // 未读标记会让它在任务列表上亮起绿/红点（"你不在时才提醒"的语义下不该亮）。
    markSessionSeen(currentSession);
    if (roundErrored) {
      roundErrored = false;  // 出错通知已随 error 事件发过，不重复
    } else {
      notifyDesktop("回合完成", "本任务的回答已结束");
    }
    dispatchNextQueued();
  } else if (t === "history_truncated") {
    // 某个标签页回退编辑删掉了一段历史：重拉时间线。本 tab 自己的回退在
    // send() 流程里已重拉过，事件重放到达时 3 秒内跳过，避免二次重拉竞态。
    if (Date.now() - lastTruncateAt > 3000 && !streaming && currentSession) {
      const keep = currentSession;
      currentSession = null;
      switchSession(keep);
    }
  } else if (t === "history_renumbered") {
    // 服务端 ord 间隔耗尽兜底：整会话重编号过，before_ord 游标指向的旧序号
    // 在新序号空间里落在哪完全随机——继续翻页会漏条目或重复。重拉整个时间线
    // （绕过 switchSession 的同 id 早退）。罕见事件，整页重建的开销可接受。
    toast("历史序号已重排，正在刷新时间线");
    const keep = currentSession;
    currentSession = null;
    switchSession(keep);
  } else if (t === "doc_created") {
    // agent 生成了一份文档：刷新列表、展开右侧面板并打开新文档
    onDocCreated(evt.name);
  } else if (t === "browser_shot") {
    // agent 的内置浏览器推来了新截图：展开右侧浏览器栏、追加最新画面
    onBrowserShot(evt.url, evt.note, evt.shot);
  } else if (t === "session_deleted") {
    // 其他标签页删掉了这个任务：收摊回到新建态
    closeEvents();
    currentSession = null;
    resetStreamState();
    chatEl.innerHTML = "";
    welcome();
    railItems = [];
    rebuildRail();  // 会话已清空：导航条收起
    closeDocsPanel();  // 会话没了，文档抽屉一并收起
    loadSessions();
    toast("该任务已在其他窗口被删除");
  } else if (t === "api_retry") {
    // API 请求瞬态失败正在退避重试：收起状态下摘要行直接可见（对标 ZCode
    // 的 apiRetry 徽标）——"正在重试(2/3)"，等待不再像卡死。重试结束后的
    // 下一轮 round/tool_call 会改写 tracePhase，无需专门复位。
    ensureTrace();
    tracePhase = `API 请求重试中（第 ${evt.attempt}/${evt.max_attempts} 次，${evt.wait}s 后）`;
    traceTick();
  } else if (t === "error") {
    flushStreamBuffers();  // 已生成的部分内容留在气泡里，再显示错误
    retireLiveBubble();
    if (traceEl) {
      traceEl.open = false;  // 出错同样收起过程；点开可排查卡在哪一步
      traceEl.querySelector("summary").textContent =
        `已工作 ${fmtElapsed((Date.now() - traceStart()) / 1000)} · 出错`;
    }
    const eb = bubble("assistant error", "❌ " + evt.message);
    // 结构化错误（errorAttribution.retryable 语义）：余额/限流/网络类错误
    // 渲染"重试"按钮——充值后一键重发，不再要用户翻出上一条消息重打一遍。
    if (evt.retryable && lastSent && lastSent.text) {
      const btn = document.createElement("button");
      btn.className = "retry-btn";
      btn.textContent = "↻ 重试上一条";
      btn.addEventListener("click", () => {
        btn.disabled = true;
        btn.textContent = "已重发";
        retryLast();
      });
      eb.appendChild(document.createElement("div"));
      eb.lastChild.appendChild(btn);
    }
    roundErrored = true;
    notifyDesktop("回合出错", summarize(evt.message, 80));
    // 生成中状态不在这里复位：turn_end 紧随 error 事件到达，由它统一收尾
  }
}

// 重试上一条：与 send() 同一条路径（重画用户气泡 + performSend）。排队中
// 则进队列；不手动清输入框——输入框在出错时早已是空的。
function retryLast() {
  if (!lastSent) return;
  if (streaming) { queueMessage(lastSent.text, lastSent.payloadAtts); return; }
  userBubble(lastSent.text, lastSent.outAtts);
  performSend({ text: lastSent.text, payloadAtts: lastSent.payloadAtts });
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
  traceEl = null;
  liveMsgs = new Map(); pendingCalls = [];
  permissionCards = new Map();
  pendingDeltas = new Map(); pendingThink = "";
  curMid = null; usageNow = null;
  qStart = 0;  // 计时起点随会话一起作废：否则切回来的新回合会接着上一个会话的时间数
  // 回退编辑态随会话一起作废：换任务后 banner 还挂着会把新消息发进错误的语境
  editTarget = null;
  const eb = $("edit-banner");
  if (eb) eb.classList.add("hidden");
}

// ---------- 桌面通知（边沿触发） ----------
// 语义沿用 ZCode useTaskNotifications：事实以事件流为准，只在【状态变化边沿】
// 且页面不在前台时发系统通知——切去干别的，长任务跑完/出错/等确认不用回来刷。
// 默认关（🔔 手动开，一次性授权）；Notification 不可用（老浏览器/拒绝授权）
// 时静默退化为无通知，绝不打扰。
const NOTIFY_KEY = "notify_desktop";
let notifyOn = localStorage.getItem(NOTIFY_KEY) === "1";

function renderBell() {
  const chip = $("bell-chip");
  chip.textContent = notifyOn ? "🔔" : "🔕";
  chip.title = notifyOn ? "桌面通知：开（仅页面在后台时提醒）" : "桌面通知：关";
}

function toggleNotify() {
  notifyOn = !notifyOn;
  localStorage.setItem(NOTIFY_KEY, notifyOn ? "1" : "0");
  if (notifyOn && "Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
  renderBell();
  toast(notifyOn ? "桌面通知已开启（仅页面在后台时提醒）" : "桌面通知已关闭");
}

function notifyDesktop(title, body) {
  if (!notifyOn || document.visibilityState === "visible") return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    new Notification(title, { body: body || "", tag: "turn-" + (currentSession || "new") });
  } catch { /* 构造失败不致命 */ }
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
let lastSent = null;    // 本 tab 最近一次成功发出的 {text, payloadAtts, outAtts}：出错重试用
let roundErrored = false;  // 本回合是否发过 error 事件：turn_end 的通知去重

let pendingProjectPath = null;  // 项目组头「＋」/chip 预选的项目：首条消息建会话时绑定

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
  attachments = [];              // 附件必须先清再存草稿：saveDraft 会把托盘快照
  renderAttachTray();            // 写回会话槽，若带着刚发出的附件存，它们会留在
                                 // __new__ 槽里，之后开任何新会话都会被恢复出来
  clearTimeout(draftTimer);      // 已发出：取消待写的防抖存盘
  saveDraft(currentSession);     // 并立即清掉该会话草稿（此刻输入框/托盘已空 → 删除）

  if (editTarget) {
    // 回退编辑的发送：先截断（服务端删该轮及其后），重拉时间线，再走常规发送。
    // 队列里有积压时拒绝——那些消息的语境随回退一起失效，自动清掉太越权。
    const target = editTarget;
    if (pendingQueue.length) { toast("有排队消息待处理，请先清理再发送回退编辑"); return; }
    editTarget = null;
    $("edit-banner").classList.add("hidden");
    const sid0 = currentSession;
    (async () => {
      try {
        await api(`/api/sessions/${sid0}/truncate`,
                  { method: "POST", body: JSON.stringify({ mid: target.mid }) });
        lastTruncateAt = Date.now();
        currentSession = null;        // 绕过 switchSession 的同 id 早退：全量重拉
        await switchSession(sid0);    // 被回退的段从时间线消失 + 重开事件流
        userBubble(text, outAtts);    // 新的一轮：自己画气泡（turn_start 是自己的 nonce，不重复画）
        performSend({ text, payloadAtts, sessionId: sid0 });
      } catch (err) {
        toast("回退失败：" + err.message);
        inputEl.value = text;         // 字还给用户，别丢
      }
    })();
    return;
  }
  if (streaming) {
    // 排队：只显示队列卡片，正式气泡等派发执行时再渲染（否则会出现两条重复消息）
    queueMessage(text, payloadAtts);
    return;
  }
  lastSent = { text, payloadAtts, outAtts };  // 出错重试的素材
  userBubble(text, outAtts);
  performSend({ text, payloadAtts, sessionId: currentSession,
                chosenPath: pendingProjectPath || undefined });
  pendingProjectPath = null;
}

function queueMessage(text, payloadAtts) {
  const item = { text, payloadAtts, sessionId: currentSession, immediate: false, el: null };
  const wrap = document.createElement("div");
  wrap.className = "bubble user queued";
  const t = document.createElement("div");
  t.textContent = text || "（仅附件）";
  wrap.appendChild(t);
  const qimgs = payloadAtts.filter(a => a.kind === "image")
    .map(a => `data:${a.mime};base64,${a.data}`);
  for (const src of qimgs) wrap.appendChild(msgImage(src, qimgs));
  if (qimgs.length > 1) wrap.classList.add("multi-img");
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
  scrollBottom(true);  // 队列卡片是用户自己的操作：永远贴底
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
  // 绑定目录只发生在【创建任务】的那次请求（创建即绑定，原子）：选目录挂起
  // 流程带 chosenPath；项目组头「＋」新建的带 pendingProjectPath。
  // 关键防线：已有会话的追问【绝不】带 workspace——pendingProjectPath 可能在
  // "新任务预选了项目但没发消息、又切进已有会话"时残留，追问带上它会被后端
  // 以"已存在的任务不支持随消息改绑目录"拒绝，消息发不出去（真实踩过的坑）。
  const creating = !target;
  const bindPath = creating
    ? (item.chosenPath || pendingProjectPath || null)
    : null;
  pendingProjectPath = null;
  const body = JSON.stringify({
    message: item.text,
    nonce: item.nonce,
    attachments: item.payloadAtts,
    // 创建即绑定项目（原子——回合启动前落库）
    workspace: (creating && bindPath) || undefined,
  });
  try {
    if (target) {
      // 追问：REST 路径（任务已存在）
      await api(`/api/sessions/${encodeURIComponent(target)}/messages`, { method: "POST", body });
    } else {
      // 新任务的第一次发送：创建任务 + 入队一步完成
      const data = await api(`/api/sessions`, { method: "POST", body });
      currentSession = data.session_id;
      localStorage.removeItem(draftKey(null));  // 新任务已实体化：清掉 __new__ 草稿位
      openEvents(currentSession);  // 立刻接事件流：turn_start 可能已在缓冲里等着补发
      loadWorkspace();             // 新任务按用户默认解析了自己的工作区，工具栏对齐
      loadAttachments();           // 新任务的附件计数（刚发送的附件此时已落盘）
      // 新任务时在下拉里选过权限模式：建会话后写入（按工作区记忆）
      if (permMode !== "confirm") {
        api(`/api/sessions/${encodeURIComponent(currentSession)}/perm_mode`,
          { method: "POST", body: JSON.stringify({ mode: permMode }) }).catch(() => {});
      }
    }
    // 附件落盘发生在后端组装消息时，这里补刷一次计数（新建/追问都适用）
    if (item.payloadAtts && item.payloadAtts.some(a => a.kind !== "image")) loadAttachments();
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

// 输入法（IME）组合态标记：中文拼音/日文假名等未上屏时，Enter 应交给输入法
// 选定候选词，绝不能触发发送。Safari 某些版本在 keydown 里 isComposing 不可靠，
// 所以额外用 compositionstart/end 维护一个标记兜底。
let imeComposing = false;
bind("input", "compositionstart", () => { imeComposing = true; });
bind("input", "compositionend", () => { imeComposing = false; });
bind("input", "keydown", (e) => {
  // e.isComposing 是标准属性；keyCode 229 是组合态下部分浏览器的兜底信号
  if (e.isComposing || imeComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
bind("attach-btn", "click", () => $("file-input").click());
bind("file-input", "change", onFilesChosen);
bind("input", "paste", onPaste);
bindDragAndDrop(document.querySelector(".composer"));
bind("send", "click", () => (streaming ? stopGeneration() : send()));
bind("new-task", "click", () => newTask());  // 顶栏"新任务"：不预绑项目（组头「＋」才带项目）
bind("model-chip", "click", toggleModelPop);
bind("manage-models", "click", openProvModal);
bind("prov-add", "click", addProv);
bind("prov-close", "click", () => $("prov-modal").classList.add("hidden"));
bind("p-save", "click", saveProv);
bind("p-test", "click", testProv);
bind("p-delete", "click", deleteProv);
bind("ws-pick", "click", openPicker);
bind("docs-chip", "click", toggleDocsPanel);
bind("attach-chip", "click", toggleAttachPop);
bind("attach-close", "click", () => $("attach-pop").classList.add("hidden"));
bind("docs-close", "click", closeDocsPanel);
bind("docs-toggle", "click", toggleDocsList);
// 浏览器栏：chip 点开/收起，✕ 关闭。与文档栏共用 --docs-w，二者互斥。
bind("bell-chip", "click", toggleNotify);
bind("edit-cancel", "click", cancelEdit);
renderBell();
bind("back-bottom", "click", () => {
  stickBottom = true;
  updateBackBottom();
  chatEl.scrollTo({ top: chatEl.scrollHeight, behavior: "smooth" });
});
bind("browser-chip", "click", () => {
  const p = browserPanelEl();
  if (!p) return;
  if (p.classList.contains("hidden")) { p.classList.remove("hidden"); closeDocsPanel(); }
  else closeBrowserPanel();
});
bind("browser-close", "click", closeBrowserPanel);
bind("m-cancel", "click", closePicker);
bind("m-close", "click", closePicker);
// 点遮罩空白处关闭（只在点到遮罩本身时，点弹窗内部不关）
bind("modal", "click", (e) => { if (e.target === $("modal")) closePicker(); });
// Esc 关闭：只在弹窗打开时响应，避免影响输入框等其他键盘行为
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modal").classList.contains("hidden")) closePicker();
});
// 「不绑定项目」：清掉预绑的项目（chip 回到"选择项目"），新任务将落进"其他"组；
// 之后再想绑定，点工具栏项目 chip 选一次即可
bind("m-skip", "click", () => {
  closePicker();
  pendingProjectPath = null;
  wsCustom = false;
  renderWsChip();
});
bind("m-up", "click", () => mParent && navTo(mParent));
bind("m-home", "click", () => navTo(mHome || undefined));
bind("m-choose", "click", chooseWorkspace);
bind("ctx-chip", "click", toggleCtxPop);
bind("perm-chip", "click", togglePermPop);
bind("user-btn", "click", toggleUserPop);
bind("pop-logout", "click", logoutNow);
bind("up-manage", "click", () => {
  $("user-pop").classList.add("hidden");
  openProvModal();
});
bind("login-submit", "click", submitLogin);
bind("login-mode", "click", () => setLoginMode(loginMode === "login" ? "register" : "login"));
// IME 组合态下 Enter 归输入法选词，不做跳转/提交
bind("login-user", "keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter") $("login-pass").focus();
});
bind("login-pass", "keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
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
  for (const [pop, btn] of [["ctx-pop", "ctx-chip"], ["model-pop", "model-chip"], ["user-pop", "user-btn"], ["git-pop", "git-chip"], ["attach-pop", "attach-chip"], ["perm-pop", "perm-chip"]]) {
    const el = $(pop);
    // git 的分支二级面板挂在外层（不在 git-pop 内）：点它不算点空白，否则
    // git 浮窗被关掉而分支面板还留着（真实踩过的坑）
    const inner = pop === "git-pop" ? e.target.closest?.("#git-branch-pop") : null;
    if (!el.classList.contains("hidden") && !el.contains(e.target) && !inner
        && !e.target.closest?.("#" + btn)) {
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

// ---------- Git 提交记录浮窗 ----------
// 数据源是后端 /api/git/*（只读，不过权限闸门）；工作区由 session_id 决定，
// 所以浮窗看到的始终是"当前任务真正在操作的目录"。
// 交互：列表 ↔ 详情两级，同一个浮窗内切换（返回按钮回到列表）。
let gitWho = "all";        // 筛选：all | me | other
let gitOffset = 0;         // 已加载条数（向上翻页游标）
const GIT_PAGE = 30;
let gitLoading = false;

function gitQs(extra = {}) {
  const qs = new URLSearchParams();
  if (currentSession) qs.set("session_id", currentSession);
  for (const [k, v] of Object.entries(extra)) qs.set(k, v);
  return qs.toString();
}

// 浮窗 / 分支按钮背后的目录名（取路径末段），未绑定时为空串。
// 注意 wsCustom 只代表"当前视图是否已绑定项目"：新任务态选了项目但还没
// 发出首条消息时，会话尚未创建，后端仍会答 no_workspace——文案靠下面的
// pendingProjectPath 区分"还没建任务"和"真的没绑项目"。
function gitWsName() {
  if (!wsCustom) return "";
  const path = $("ws-pick") ? $("ws-pick").title : "";
  return (path || "").replace(/\/+$/, "").split("/").pop() || "";
}

// 未绑定项目时给可操作的引导（而不是干巴巴一句"没有绑定"）：新任务已预选
// 项目 → 让用户先把消息发出去；否则 → 提示点工具栏选目录。
function gitPickHint() {
  if (!wsCustom) return pendingProjectPath
    ? "项目已选好，发送第一条消息后即可查看提交记录"
    : "这个任务还没有项目文件夹：点输入框上方的「📁 选择项目」选一个目录，再点这里就能看提交记录";
  return "";
}

// 浮窗在任务尚未创建时打开（工具栏预选了项目还没发消息）：此时没有
// session_id，后端无从解析工作区，列表注定是空的——直接显示引导。
function renderGitNoSession() {
  $("git-body").innerHTML =
    `<div class="git-empty">${esc(gitPickHint() || "任务还没有创建，发送第一条消息后再查看提交记录")}</div>`;
}

// 把分支名写进右上角触发按钮。传空串 = 不是仓库/未绑定项目，按钮退回
// 只显示 "Git"，并去掉 warn 之外的状态。
function setGitChipBranch(branch) {
  const el = $("git-chip-branch");
  if (!el) return;
  // 标签只写"目录名 @ 分支"：此前只写分支名，多个项目都在 main 上时根本
  // 看不出这个按钮说的是哪个仓库；长目录名在 CSS 里截断（.git-chip-branch）。
  const wsName = branch ? gitWsName() : "";
  el.textContent = branch ? (wsName ? `${wsName} @ ${branch}` : branch) : "Git";
  el.title = branch
    ? `当前项目文件夹：${$("ws-pick").title}（分支 ${branch}，点击查看提交记录）`
    : "查看当前项目文件夹的 Git 提交记录";
}

// 后台轻量刷新按钮上的分支名：不需要打开浮窗也能看到"我在哪个分支"。
// 只在已绑定项目时发请求；失败静默（不是仓库属正常状态，不该弹错）。
async function refreshGitChip() {
  if (!currentSession) {
    // 新任务态：会话未创建。预选了项目（组头「＋」/chip）时也立刻显示该项目
    // 的分支名——后端支持 workspace 直传（仅预览，不用等首条消息建会话）。
    const qs = new URLSearchParams();
    if (pendingProjectPath) qs.set("workspace", pendingProjectPath);
    if (!qs.toString()) {
      setGitChipBranch("");
      $("git-chip").classList.remove("warn");
      return;
    }
    try {
      const d = await api("/api/git/summary?" + qs.toString());
      setGitChipBranch(d.ok ? d.branch : "");
      $("git-chip").classList.toggle("warn", !d.ok);
    } catch (e) {
      setGitChipBranch("");
    }
    return;
  }
  try {
    const d = await api("/api/git/summary?" + gitQs());
    setGitChipBranch(d.ok ? d.branch : "");
    $("git-chip").classList.toggle("warn", !d.ok);
  } catch (e) {
    setGitChipBranch("");
  }
}

function toggleGitPop() {
  const pop = $("git-pop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  restoreGitSize();
  positionGitPop();
  pop.classList.remove("hidden");
  gitWho = "all";
  syncGitFilter();
  gitOffset = 0;
  if (!currentSession) { renderGitNoSession(); return; }
  loadGitList(true);
}

// 浮窗贴在触发按钮下方；右侧留 16px 边距，避免贴边
function positionGitPop() {
  const pop = $("git-pop"), btn = $("git-chip");
  const rect = btn.getBoundingClientRect();
  const w = Math.min(720, innerWidth - 32);
  pop.style.width = w + "px";
  pop.style.left = Math.max(16, Math.min(rect.right - w, innerWidth - w - 16)) + "px";
  pop.style.top = (rect.bottom + 8) + "px";
}

function syncGitFilter() {
  for (const b of document.querySelectorAll(".git-fbtn")) {
    b.classList.toggle("active", b.dataset.who === gitWho);
  }
}

async function loadGitList(reset = false) {
  if (gitLoading) return;
  gitLoading = true;
  const body = $("git-body");
  if (reset) body.innerHTML = '<div class="git-loading">加载中…</div>';
  try {
    const qs = gitQs({ limit: GIT_PAGE, offset: gitOffset, author: gitWho });
    const data = await api("/api/git/log?" + qs);
    renderGitList(data, reset);
  } catch (e) {
    // 追加失败时不能把已有内容清空：只提示，保留用户已看到的列表
    if (reset) body.innerHTML = `<div class="git-empty">加载失败：${esc(e.message)}</div>`;
    else toast("加载更多失败：" + e.message);
  } finally {
    gitLoading = false;
  }
}

function renderGitList(data, reset) {
  const body = $("git-body");
  const branchBtn = $("git-branch");
  branchBtn.textContent = (data.branch || "") + " ▾";
  branchBtn.classList.toggle("hidden", !data.ok || !data.branch);
  $("git-chip").classList.toggle("warn", !data.ok);
  // 头部标出这份列表读的是哪个项目文件夹：多个任务各绑不同项目时，
  // 只靠分支名分不清是哪个仓库（都在 main 上时尤其明显）。
  const scope = $("git-scope");
  const wsPath = $("ws-pick") ? $("ws-pick").title : "";
  scope.textContent = data.ok && wsPath ? `📁 ${wsPath}` : "";
  scope.title = scope.textContent;
  setGitChipBranch(data.ok ? data.branch : "");
  // 底部身份行：告诉用户"我"是按哪个 git 身份判定的
  const id = data.identity || {};
  $("git-identity").textContent = id.email ? `本机身份：${id.name || id.email}` : "";
  $("git-dirty").textContent = data.dirty ? `${data.dirty} 处未提交改动` : "";

  if (!data.ok) {
    // not_repo / no_workspace：都是正常状态，给引导文案而非报错。
    // 未绑定项目时后端只回一句"还没有绑定项目文件夹"，用户看不出下一步该
    // 干什么——这里换成可操作提示（选目录 / 先把首条消息发出去）。
    const hint = data.reason === "no_workspace" ? gitPickHint() : "";
    body.innerHTML = `<div class="git-empty">${esc(hint || data.error || "无法读取 Git 信息")}</div>`;
    return;
  }
  const commits = data.commits || [];
  if (reset) body.innerHTML = "";
  if (!commits.length) {
    const tip = gitWho === "me" ? "没有你提交的记录"
              : gitWho === "other" ? "没有他人提交的记录"
              : "这个仓库还没有提交";
    body.innerHTML = `<div class="git-empty">${tip}</div>`;
    return;
  }
  // 追加前先摘掉上一页遗留的"加载更多"：否则每次翻页都会再挂一个，
  // 按钮越堆越多（旧按钮还指向过期的 offset）
  const oldMore = body.querySelector(".git-more");
  if (oldMore) oldMore.remove();
  const frag = document.createDocumentFragment();
  for (const c of commits) frag.appendChild(gitRow(c));
  body.appendChild(frag);
  gitOffset += commits.length;
  // 本页取满一页才可能还有下一页（取不满说明已到尾部）
  if (commits.length >= GIT_PAGE) {
    const more = document.createElement("button");
    more.className = "git-more";
    more.textContent = "加载更多…";
    more.onclick = (e) => { e.stopPropagation(); loadGitList(false); };
    body.appendChild(more);
  }
}

function gitRow(c) {
  const row = document.createElement("button");
  row.className = "git-row";
  const l1 = document.createElement("div");
  l1.className = "git-row-l1";
  const hash = document.createElement("span");
  hash.className = "git-row-hash";
  hash.textContent = c.short;
  const author = document.createElement("span");
  author.className = "git-row-author";
  author.textContent = c.author;
  const date = document.createElement("span");
  date.className = "git-row-date";
  date.textContent = gitTimeAgo(c.date);
  l1.append(hash, author);
  if (c.mine) {
    const mine = document.createElement("span");
    mine.className = "git-mine";
    mine.textContent = "我";
    l1.appendChild(mine);
  }
  l1.appendChild(date);

  const subj = document.createElement("div");
  subj.className = "git-row-subj";
  subj.textContent = c.subject || "(无提交信息)";
  // 增删行数：贴在同一行右侧（小字、红绿）
  if (c.added || c.removed) {
    const stat = document.createElement("span");
    stat.className = "git-row-stat";
    stat.innerHTML = `<span class="add">+${c.added}</span> <span class="del">−${c.removed}</span>`;
    subj.appendChild(stat);
  }
  row.append(l1, subj);
  // stopPropagation：阻止冒泡到 document 的"点浮层外关闭"处理器。
  // 行内是 <span>，点它们时 e.target 是 span 而非按钮本身——虽然
  // closest()/contains() 理论上能兜住，但显式停掉冒泡最稳妥：
  // 详情视图就在同一个浮窗里切换，绝不该因为一次点击把浮窗关掉。
  row.onclick = (e) => { e.stopPropagation(); openGitDetail(c.hash); };
  return row;
}

async function openGitDetail(hash) {
  const body = $("git-body");
  body.innerHTML = '<div class="git-loading">加载提交详情…</div>';
  let data;
  try {
    data = await api("/api/git/show?" + gitQs({ hash }));
  } catch (e) {
    body.innerHTML = `<div class="git-empty">加载失败：${esc(e.message)}</div>`;
    return;
  }
  body.innerHTML = "";
  body.appendChild(gitDetailHead(data));
  if (data.body) {
    const b = document.createElement("div");
    b.className = "git-detail-body";
    b.textContent = data.body;
    body.appendChild(b);
  }
  // merge 提交：git 默认不产出 patch，这里给出说明而不是留一片空白
  if (data.is_merge && !(data.files || []).length) {
    const n = document.createElement("div");
    n.className = "git-merge-note";
    n.textContent = `这是合并提交（${(data.parents || []).length} 个父提交）。git 无法自动选一侧对比，本次合并带来的改动请查看被合入的那条提交。`;
    body.appendChild(n);
  }
  for (const f of data.files || []) body.appendChild(gitFileBlock(f));
  if (data.files_truncated) {
    const m = document.createElement("div");
    m.className = "git-more-note";
    m.textContent = `（共 ${data.file_count} 个文件，仅显示前 ${(data.files || []).length} 个）`;
    body.appendChild(m);
  }
  body.scrollTop = 0;
}

function gitDetailHead(data) {
  const head = document.createElement("div");
  head.className = "git-detail-head";
  const back = document.createElement("button");
  back.className = "git-back";
  back.textContent = "← 返回";
  back.onclick = (e) => { e.stopPropagation(); gitOffset = 0; loadGitList(true); };
  const meta = document.createElement("span");
  meta.className = "git-detail-meta";
  meta.textContent = `${data.short} · ${data.author} · ${gitTimeAgo(data.date)}`;
  head.append(back, meta);
  return head;
}

// 单个文件的 diff 块：复用执行过程里那套 .diff / .diff-line 样式，
// 保证"写入卡"和"提交详情"两处的红绿观感一致。
function gitFileBlock(f) {
  const box = document.createElement("div");
  box.className = "git-file";
  const head = document.createElement("div");
  head.className = "git-file-head";
  const p = document.createElement("span");
  p.className = "path";
  p.textContent = f.path;
  const st = document.createElement("span");
  st.className = "stat";
  st.innerHTML = `<span class="add">+${f.added}</span> <span class="del">−${f.removed}</span>`;
  head.append(p, st);
  box.appendChild(head);

  const diff = document.createElement("div");
  diff.className = "diff";
  for (const h of f.hunks || []) {
    const hh = document.createElement("div");
    hh.className = "git-hunk-head";
    hh.textContent = h.header;
    diff.appendChild(hh);
    for (const ln of h.lines || []) {
      const row = document.createElement("div");
      row.className = "diff-line " + ln.t;
      const mark = document.createElement("span");
      mark.className = "diff-sign";
      mark.textContent = ln.t === "add" ? "+" : ln.t === "del" ? "−" : " ";
      const txt = document.createElement("span");
      txt.className = "diff-text";
      txt.textContent = ln.text || " ";
      row.append(mark, txt);
      diff.appendChild(row);
    }
  }
  if (f.truncated) {
    const t = document.createElement("div");
    t.className = "git-more-note";
    t.textContent = "（该文件改动过大，仅显示前部分）";
    diff.appendChild(t);
  }
  box.appendChild(diff);
  return box;
}

// 提交时间的相对表达：今天显示时刻，昨天/更早显示天数（git 给的是 ISO 串）
function gitTimeAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t)) return iso;
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
}

// 纯文本转义：提交信息/文件名都是仓库里的外部内容，一律走 textContent 或
// 转义后再拼——绝不把未转义字符串塞进 innerHTML。
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 触发按钮与筛选按钮的事件绑定
$("git-chip").addEventListener("click", toggleGitPop);
$("git-close").addEventListener("click", () => $("git-pop").classList.add("hidden"));
for (const b of document.querySelectorAll(".git-fbtn")) {
  b.addEventListener("click", () => {
    gitWho = b.dataset.who;
    syncGitFilter();
    gitOffset = 0;
    loadGitList(true);
  });
}
window.addEventListener("resize", () => {
  if (!$("git-pop").classList.contains("hidden")) positionGitPop();
});

// ---------- 分支切换 ----------
// 点浮窗头部的分支名弹出小面板；选中即调 /api/git/checkout 真实切分支。
// checkout 是本功能唯一的 git 写操作（会改工作区文件），因此：
//   * 分支名后端会做本地分支白名单校验，这里不做任何拼接；
//   * 有未提交改动时 git 会拒绝，错误原话直接 toast 给用户，不静默处理。
function toggleBranchPop() {
  const pop = $("git-branch-pop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  positionBranchPop();
  pop.classList.remove("hidden");
  loadBranches();
}

function positionBranchPop() {
  const pop = $("git-branch-pop"), btn = $("git-branch");
  const rect = btn.getBoundingClientRect();
  pop.style.left = Math.max(8, rect.left) + "px";
  pop.style.top = (rect.bottom + 6) + "px";
}

async function loadBranches() {
  const list = $("git-branch-list");
  list.innerHTML = '<div class="git-loading">加载中…</div>';
  try {
    const data = await api("/api/git/branches?" + gitQs());
    list.innerHTML = "";
    if (!data.ok) {
      list.innerHTML = `<div class="git-empty">${esc(data.error || "无法读取分支")}</div>`;
      return;
    }
    for (const b of data.branches || []) list.appendChild(branchItem(b));
  } catch (e) {
    list.innerHTML = `<div class="git-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function branchItem(b) {
  const el = document.createElement("button");
  el.className = "gbp-item" + (b.current ? " current" : "");
  el.textContent = b.name;
  if (b.current) {
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.textContent = "✓";
    el.appendChild(tick);
  }
  // 当前分支不可点（切自己无意义）；其余点击即切换。
  // stopPropagation：分支面板在 git-pop 之外，不拦的话这次点击会被
  // document 的"点浮层外关闭"处理器认作点了外部，把 git-pop 一起关掉。
  el.onclick = b.current ? null : (e) => { e.stopPropagation(); doCheckout(b.name, el); };
  return el;
}

async function doCheckout(branch, el) {
  el.classList.add("busy");
  try {
    const r = await api("/api/git/checkout", {
      method: "POST",
      body: JSON.stringify({ session_id: currentSession, branch }),
    });
    if (!r.ok) {
      // git 拒绝（未提交改动冲突等）：把原话给用户看
      toast("切换失败：" + (r.error || "未知原因"));
      el.classList.remove("busy");
      return;
    }
    $("git-branch-pop").classList.add("hidden");
    toast(`已切换到分支 ${branch}`);
    // 分支变了：提交列表与分支徽章都要重拉
    gitOffset = 0;
    loadGitList(true);
  } catch (e) {
    toast("切换失败：" + e.message);
    el.classList.remove("busy");
  }
}

// 点分支面板外任意处关闭（与其它浮层一致）
document.addEventListener("click", (e) => {
  const pop = $("git-branch-pop");
  if (pop.classList.contains("hidden")) return;
  if (!pop.contains(e.target) && !e.target.closest?.("#git-branch")) {
    pop.classList.add("hidden");
  }
});
$("git-branch").addEventListener("click", (e) => { e.stopPropagation(); toggleBranchPop(); });

// 点附件目录浮层外任意处关闭：目录浮层挂在 body 上（不受 attach-pop 裁剪），
// 与其他浮层同一套「点空白收起」约定；点 ☰ 按钮自身由 stopPropagation 排除。
document.addEventListener("click", (e) => {
  const pop = $("attach-toc-pop");
  if (!pop || pop.classList.contains("hidden")) return;
  if (!pop.contains(e.target) && !e.target.closest?.(".attach-toc-btn")) {
    closeAttachToc();
  }
});

// ---------- 启动 ----------
function boot() {  // 登录成功（或刷新后 token 仍有效）后的页面初始化；切用户时先清现场
  currentSession = null;
  chatEl.innerHTML = "";
  historyMids = new Set();  // 上一个用户/任务的去重基准作废
  railItems = [];
  rebuildRail();            // 现场已清：导航条收起
  $("login-page").classList.add("hidden");   // 离开登录页
  $("layout").classList.remove("hidden");    // 进入对话页
  setWho(who);
  welcome();
  loadConfig();
  loadWorkspace();
  loadSessions();
  startStatePolling();  // 列表状态徽标的低频刷新（见 startStatePolling）
  restoreDocsWidth();       // 恢复上次的文档栏宽度
  restoreDocsListState();   // 恢复文档列表的折叠态
  bindDocsResizer();        // 文档栏左缘的拖拽把手
  initSidebar();            // 侧栏收起/展开 + 收起后左缘悬浮唤出
  restoreSidebarWidth();    // 恢复上次的侧栏宽度
  bindSideResizer();        // 侧栏右缘的拖拽条
  bindAttachResizer();      // 附件浮窗左下角的拖拽把手
  bindAttachListControls(); // 附件浮窗：列表宽度拖拽 + 列表收起/展开
  bindGitResizer();         // Git 浮窗左下角的拖拽把手
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

// ---------- 左侧会话栏：收起/展开 + 收起后的悬浮唤出 ----------
// 展开态：正常占位（flex 一列），点标题栏的 ⇤ 收起。
// 收起态：宽度收 0 让出空间，并在左边缘留一条热区；鼠标滑到最左侧
//         → 会话栏以悬浮层滑入（浮在主区之上，不挤压对话区），
//         鼠标移出（热区或悬浮层）→ 自动滑走。
const SIDE_COLLAPSED_KEY = "sidebarCollapsed";
const SIDE_LEAVE_DELAY = 260;   // 鼠标移出后延迟收起：给"滑向悬浮层"留出过渡时间

function sidebarEl() { return document.querySelector(".sidebar"); }

function initSidebar() {
  const side = sidebarEl();
  const btn = $("side-collapse");
  const hover = $("side-hover");
  const mask = $("side-mask");
  const nav = $("nav-toggle");
  if (!side || !btn || !hover) return;

  const setCollapsed = (collapsed) => {
    side.classList.toggle("collapsed", collapsed);
    // 收起态才让悬浮层生效；展开态清掉 floating/peek，回到普通占位布局
    if (!collapsed) side.classList.remove("floating", "peek");
    else side.classList.add("floating");
    hover.classList.toggle("hidden", !collapsed);
    btn.textContent = collapsed ? "⇥" : "⇤";
    btn.title = collapsed ? "展开侧栏" : "收起侧栏";
    localStorage.setItem(SIDE_COLLAPSED_KEY, collapsed ? "1" : "0");
  };

  const isCollapsed = () => side.classList.contains("collapsed");
  const peek = () => { if (isCollapsed()) side.classList.add("peek"); };
  const unpeek = () => side.classList.remove("peek");

  // ---------- 手机端：侧栏改成左滑抽屉 ----------
  // 手机的交互与桌面不是一套：触屏没有 hover（桌面那套"左缘热区唤出"会失效，
  // 而且 hover 在触屏上是"点了才粘住"），所以手机上走独立分支——
  // ☰ 打开 / 点遮罩或选中任务后收起，不复用桌面的收起态。
  const openDrawer = () => {
    side.classList.add("drawer-open");
    if (mask) mask.classList.remove("hidden");
  };
  const closeDrawer = () => {
    side.classList.remove("drawer-open");
    if (mask) mask.classList.add("hidden");
  };
  const isDrawerOpen = () => side.classList.contains("drawer-open");

  // 模式切换（旋转屏幕 / 缩放窗口）时重排本侧交互：两套状态互不残留，
  // 桌面切回桌面时回到用户上次保存的收起态。
  const applyMode = () => {
    closeDrawer();
    if (isMobileMode()) {
      side.classList.remove("collapsed", "floating", "peek");
      hover.classList.add("hidden");
      btn.textContent = "✕";
      btn.title = "关闭任务列表";
    } else {
      setCollapsed(localStorage.getItem(SIDE_COLLAPSED_KEY) === "1");
    }
  };

  btn.addEventListener("click", () => {
    if (isMobileMode()) return closeDrawer();
    setCollapsed(!isCollapsed());
  });
  if (nav) nav.addEventListener("click", () => (isDrawerOpen() ? closeDrawer() : openDrawer()));
  if (mask) mask.addEventListener("click", closeDrawer);

  // 鼠标进入左缘热区 → 弹出（触屏不会触发 mouseenter，无需在 JS 里分支）
  hover.addEventListener("mouseenter", peek);
  // 鼠标进入悬浮层 → 保持展开（取消可能已排队的收起）
  let leaveTimer = null;
  const cancelLeave = () => { if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null; } };
  const scheduleLeave = () => {
    cancelLeave();
    leaveTimer = setTimeout(() => { unpeek(); leaveTimer = null; }, SIDE_LEAVE_DELAY);
  };
  hover.addEventListener("mouseleave", scheduleLeave);
  side.addEventListener("mouseenter", cancelLeave);
  side.addEventListener("mouseleave", () => { if (isCollapsed()) scheduleLeave(); });
  // 在侧栏里点了某条任务/新任务后立刻收起，别挡住对话（桌面=收起悬浮层，手机=关抽屉）
  side.addEventListener("click", (e) => {
    const hit = e.target.closest(".task") || e.target.closest("#new-task");
    if (isMobileMode()) { if (hit) closeDrawer(); return; }
    if (!isCollapsed()) return;
    if (hit) unpeek();
  });
  // Esc 收起悬浮层 / 关抽屉
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (isDrawerOpen()) return closeDrawer();
    if (isCollapsed()) unpeek();
  });

  applyMode();
  document.addEventListener("modechange", applyMode);
}

// ---------- 侧栏宽度拖拽 ----------
// 布局是 sidebar + main 并列：在 sidebar 右缘加一条 4px resizer，拖动改
// :root 上的 --side-w 变量。180~480px 夹紧；窄屏媒体查询里 resizer 被隐藏，
// 事件不会触发，无需额外判断。
const SIDE_W_KEY = "sideWidth";
const SIDE_W_MIN = 180, SIDE_W_MAX = 480;

function clampSideW(w) {
  return Math.max(SIDE_W_MIN, Math.min(SIDE_W_MAX, w));
}

function restoreSidebarWidth() {
  const saved = parseInt(localStorage.getItem(SIDE_W_KEY) || "", 10);
  if (Number.isFinite(saved)) {
    document.documentElement.style.setProperty("--side-w", clampSideW(saved) + "px");
  }
}

function bindSideResizer() {
  const handle = $("side-resizer");
  if (!handle) return;
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = document.querySelector(".sidebar").getBoundingClientRect().width;
    document.body.classList.add("side-resizing");  // CSS 里借此禁用 width 过渡：拖动要 1:1 跟手
    const onMove = (ev) => {
      const w = clampSideW(startW + (ev.clientX - startX)); // 向右拖 → 变宽
      document.documentElement.style.setProperty("--side-w", w + "px");
    };
    const onUp = () => {
      document.body.classList.remove("side-resizing");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const cur = clampSideW(document.querySelector(".sidebar").getBoundingClientRect().width);
      localStorage.setItem(SIDE_W_KEY, String(cur));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// ---------- 会话附件浮窗 ----------
// 用户视角的查看器：只看【已经上传并落盘】的文件类附件（data/attachments 或
// 工作区 .coding-agent/attachments）。与工具栏 📎 的区别是语义，不是位置：
//   📎 + attach-tray = 上传动作与待发队列（本地 → 会话，发送后清空）；
//   本浮窗          = 会话里已有什么（只读，不能上传/删除/编辑）。
// 图片附件不落盘（走 base64 直接进消息），因此不在本浮窗内——这里只列文件类。
let attachItems = [];        // 当前会话的附件列表缓存 [{name, bytes, mtime}]
let attachActive = "";       // 当前打开的文件名（列表高亮用）

async function loadAttachments() {
  const countEl = $("attach-count");
  if (!currentSession) {
    attachItems = [];
    if (countEl) countEl.textContent = "0";
    return;
  }
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}/attachments`);
    attachItems = data.attachments || [];
    if (countEl) countEl.textContent = String(attachItems.length);
  } catch (e) { /* 计数是次要信息，失败不打扰用户 */ }
}

// ---------- 附件浮窗：列表宽度拖拽 + 列表收起 ----------
// 列表 ↔ 正文之间是 4px 分栏条，拖动改 .attach-pop 上的 --attach-list-w
// 变量；头部 ◧ 按钮整列收起/展开，两者状态都持久化。
const ATTACH_LIST_W_KEY = "attachListW", ATTACH_COLLAPSED_KEY = "attachListCollapsed";
const ATTACH_LIST_W_MIN = 140, ATTACH_LIST_W_MAX = 480;

function bindAttachListControls() {
  const pop = $("attach-pop"), split = $("attach-split");
  const toggle = $("attach-list-toggle");
  if (!pop || !split || !toggle) return;

  if (localStorage.getItem(ATTACH_COLLAPSED_KEY) === "1") {
    pop.classList.add("list-collapsed");
    toggle.title = "展开文件列表";
  }

  toggle.addEventListener("click", () => {
    const collapsed = pop.classList.toggle("list-collapsed");
    toggle.title = collapsed ? "展开文件列表" : "收起文件列表";
    localStorage.setItem(ATTACH_COLLAPSED_KEY, collapsed ? "1" : "0");
  });

  split.addEventListener("mousedown", (e) => {
    e.preventDefault();
    if (pop.classList.contains("list-collapsed")) return;
    const startX = e.clientX;
    const startW = $("attach-list").getBoundingClientRect().width;
    document.body.classList.add("attach-splitting");
    const onMove = (ev) => {
      const w = Math.max(ATTACH_LIST_W_MIN,
        Math.min(ATTACH_LIST_W_MAX, startW + (ev.clientX - startX)));
      pop.style.setProperty("--attach-list-w", w + "px");
    };
    const onUp = () => {
      document.body.classList.remove("attach-splitting");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const cur = $("attach-list").getBoundingClientRect().width;
      localStorage.setItem(ATTACH_LIST_W_KEY, String(Math.round(cur)));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  const savedW = parseInt(localStorage.getItem(ATTACH_LIST_W_KEY) || "", 10);
  if (Number.isFinite(savedW)) {
    pop.style.setProperty("--attach-list-w",
      Math.max(ATTACH_LIST_W_MIN, Math.min(ATTACH_LIST_W_MAX, savedW)) + "px");
  }
}

// ---------- Git 浮窗大小拖拽 ----------
// 与附件浮窗同款左下角把手：宽高持久化，未拖过时用 CSS 默认尺寸。
const GIT_W_KEY = "gitPopW", GIT_H_KEY = "gitPopH";
const GIT_W_MIN = 480, GIT_H_MIN = 320;

function clampGitW(w) { return Math.max(GIT_W_MIN, Math.min(innerWidth - 32, w)); }
function clampGitH(h) { return Math.max(GIT_H_MIN, Math.min(innerHeight - 80, h)); }

// 打开时若保存过尺寸则恢复；positionGitPop 只设 left/top，不覆盖宽度之外的高度
function restoreGitSize() {
  const pop = $("git-pop");
  const w = parseInt(localStorage.getItem(GIT_W_KEY) || "", 10);
  const h = parseInt(localStorage.getItem(GIT_H_KEY) || "", 10);
  if (Number.isFinite(w)) pop.style.width = clampGitW(w) + "px";
  if (Number.isFinite(h)) pop.style.height = clampGitH(h) + "px";
}

function bindGitResizer() {
  const pop = $("git-pop");
  if (!pop) return;
  bindEdgeResize(pop, {
    clampW: clampGitW, clampH: clampGitH,
    onEnd: (w, h) => {
      localStorage.setItem(GIT_W_KEY, String(w));
      localStorage.setItem(GIT_H_KEY, String(h));
    },
  });
}

// 切会话/新任务时复位：收起浮窗并清空内容（对齐 resetDocsPanel 的做法）
function resetAttachPop() {
  const pop = $("attach-pop");
  if (pop) pop.classList.add("hidden");
  const list = $("attach-list");
  if (list) list.innerHTML = "";
  const view = $("attach-view");
  if (view) view.innerHTML = "";
  attachItems = [];
  attachActive = "";
  const countEl = $("attach-count");
  if (countEl) countEl.textContent = "0";
}

function toggleAttachPop() {
  const pop = $("attach-pop");
  if (!pop) return;
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  $("git-pop").classList.add("hidden");  // 与 git 浮窗互斥，不叠层
  positionAttachPop();
  pop.classList.remove("hidden");
  renderAttachList();
}

// 贴在触发按钮下方；右侧留 16px 边距，避免贴边（与 positionGitPop 同套路）。
// 用户拖过大小后（localStorage 有记录）沿用保存的宽高，且保持水平锚点不跳动。
const ATTACH_W_KEY = "attachPopW", ATTACH_H_KEY = "attachPopH";
const ATTACH_W_MIN = 480, ATTACH_H_MIN = 320;

function clampAttachW(w) { return Math.max(ATTACH_W_MIN, Math.min(innerWidth - 32, w)); }
function clampAttachH(h) { return Math.max(ATTACH_H_MIN, Math.min(innerHeight - 80, h)); }

function positionAttachPop() {
  const pop = $("attach-pop"), btn = $("attach-chip");
  if (!pop || !btn) return;
  const rect = btn.getBoundingClientRect();
  const savedW = parseInt(localStorage.getItem(ATTACH_W_KEY) || "", 10);
  const savedH = parseInt(localStorage.getItem(ATTACH_H_KEY) || "", 10);
  const w = Number.isFinite(savedW) ? clampAttachW(savedW) : Math.min(760, innerWidth - 32);
  const h = Number.isFinite(savedH) ? clampAttachH(savedH) : null;
  pop.style.width = w + "px";
  if (h) pop.style.height = h + "px";
  // 锚点：优先按触发按钮左缘对齐；拖宽后若右边距不够则往左收
  pop.style.left = Math.max(16, Math.min(rect.left, innerWidth - w - 16)) + "px";
  pop.style.top = (rect.bottom + 8) + "px";
}

// 隐形边缘拖拽（所有浮窗通用）：浮窗的【左缘】与【下缘】各是一条 6px 的
// 透明热区（不画任何把手标志），鼠标靠上去变对应方向的 resize 光标。
// 左缘左右拖 = 变宽（右缘钉住）；下缘上下拖 = 变高（顶缘钉住）；左下角
// 两方向同时生效。松开时把最终尺寸交给 onEnd 持久化。
function bindEdgeResize(pop, opts) {
  const { clampW, clampH, onEnd } = opts;
  const EDGE = 6;  // 热区厚度
  // 左下角斜向热区：独立元素盖在两条边热区之上（同为角部，避免被左缘热区
  // 挡住落点），拖动时宽高同时跟随，光标是斜向箭头。
  const corner = document.createElement("div");
  corner.className = "pop-edge pop-edge-corner";
  pop.appendChild(corner);
  corner.addEventListener("mousedown", (e) => startDrag(e, "wh"));
  for (const edge of ["w", "h"]) {
    const zone = document.createElement("div");
    zone.className = `pop-edge pop-edge-${edge}`;
    pop.appendChild(zone);
    zone.addEventListener("mousedown", (e) => startDrag(e, edge));
  }

  function startDrag(e, dirs) {  // dirs: "w" | "h" | "wh"（wh = 两方向同时）
    e.preventDefault();
    e.stopPropagation();  // 别冒泡进「点空白关浮窗」
    const startX = e.clientX, startY = e.clientY;
    const rect = pop.getBoundingClientRect();
    document.body.classList.add("pop-resizing");
    const onMove = (ev) => {
      if (dirs.includes("w")) {
        const w = clampW(rect.width + (startX - ev.clientX));  // 左拽 = 变宽
        pop.style.width = w + "px";
        pop.style.left = Math.max(4, rect.right - w) + "px";   // 右缘钉住
      }
      if (dirs.includes("h")) {
        pop.style.height = clampH(rect.height + (ev.clientY - startY)) + "px";  // 下拽 = 变高
      }
    };
    const onUp = () => {
      document.body.classList.remove("pop-resizing");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const r = pop.getBoundingClientRect();
      onEnd(Math.round(r.width), Math.round(r.height));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }
}

function bindAttachResizer() {
  const pop = $("attach-pop");
  if (!pop) return;
  bindEdgeResize(pop, {
    clampW: clampAttachW, clampH: clampAttachH,
    onEnd: (w, h) => {
      localStorage.setItem(ATTACH_W_KEY, String(w));
      localStorage.setItem(ATTACH_H_KEY, String(h));
    },
  });
}

function renderAttachList() {
  const list = $("attach-list");
  const view = $("attach-view");
  if (!list) return;
  list.innerHTML = "";
  $("attach-scope").textContent = attachItems.length
    ? `${attachItems.length} 个文件 · ${fmtBytes(attachItems.reduce((s, a) => s + a.bytes, 0))}`
    : "";
  if (!currentSession) {
    view.innerHTML = '<div class="attach-empty">任务还没有创建，发送第一条消息后再查看附件</div>';
    return;
  }
  if (!attachItems.length) {
    view.innerHTML = '<div class="attach-empty">本会话还没有上传过文件附件。'
      + '图片附件直接随消息发送，不会出现在这里。</div>';
    return;
  }
  for (const it of attachItems) {
    const li = document.createElement("li");
    li.className = "attach-item";
    li.dataset.name = it.name;
    const name = document.createElement("span");
    name.className = "attach-name";
    name.textContent = it.name;
    name.title = it.name;
    const meta = document.createElement("span");
    meta.className = "attach-meta";
    meta.textContent = `${fmtBytes(it.bytes)} · ${fmtDocTime(it.mtime)}`;
    li.append(name, meta);
    li.addEventListener("click", () => openAttachment(it.name));
    // Markdown 附件才有目录可看：hover 时在文件名右侧露出 ☰ 入口
    if (/\.md$/i.test(it.name || "")) {
      const tocBtn = document.createElement("span");
      tocBtn.className = "attach-toc-btn";
      tocBtn.textContent = "☰";
      tocBtn.title = "查看文档目录";
      tocBtn.addEventListener("click", (e) => {
        e.stopPropagation();  // 别触发 li 的「打开这份附件」
        if (attachActive !== it.name) openAttachment(it.name).then(() => toggleAttachToc(it.name));
        else toggleAttachToc(it.name);
      });
      li.appendChild(tocBtn);
    }
    li.appendChild(makeDeleteBtn("attach-del-btn", it.name, deleteAttachment));
    list.appendChild(li);
  }
  if (attachActive) openAttachment(attachActive);
}

// 删除按钮（附件浮窗/文档面板共用）：二次确认交互——
// 第一次点进入确认态（变红显示"确认?"），再点才真删；2.5 秒不点自动退回。
// 之所以不用 confirm() 对话框：原生弹窗样式突兀，且行内确认让"删的是哪个
// 文件"看得见，不会弹窗文案与目标对不上号。
function makeDeleteBtn(cls, name, delFn) {
  const btn = document.createElement("span");
  btn.className = cls;
  btn.textContent = "🗑";
  btn.title = "删除";
  let timer = null;
  const disarm = () => {
    btn.classList.remove("confirm");
    btn.textContent = "🗑";
    if (timer) { clearTimeout(timer); timer = null; }
  };
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();  // 别触发列表项的「打开这份文件」
    if (!btn.classList.contains("confirm")) {
      btn.classList.add("confirm");
      btn.textContent = "确认?";
      timer = setTimeout(disarm, 2500);  // 犹豫即放弃
      return;
    }
    disarm();
    try {
      await delFn(name);
      toast(`已删除《${name}》`);
    } catch (err) {
      toast("删除失败：" + err.message);
    }
  });
  btn.addEventListener("mouseleave", () => {
    // 鼠标移走后保留确认态一小会儿，期间点回来仍生效；超时自动退回
    if (btn.classList.contains("confirm") && !timer) timer = setTimeout(disarm, 2500);
  });
  return btn;
}

// 删除一份会话附件：调 DELETE 接口 → 刷新列表与计数 → 若正预览着它就清空右侧。
async function deleteAttachment(name) {
  await api(`/api/sessions/${encodeURIComponent(currentSession)}/attachments?name=${encodeURIComponent(name)}`,
    { method: "DELETE" });
  attachItems = attachItems.filter((a) => a.name !== name);
  const countEl = $("attach-count");
  if (countEl) countEl.textContent = String(attachItems.length);
  if (attachActive === name) {
    attachActive = null;
    $("attach-view").innerHTML = '<div class="attach-empty">附件已删除。</div>';
  }
  renderAttachList();
}

// 打开一份附件：文本按原文渲染（等宽 <pre>，不解析 Markdown——看日志与代码
// 就该是原样）；压缩包列成员清单；二进制明确说"不能在这儿看"。
// ---------- 附件 Markdown 目录（TOC）----------
// 列表项 hover 露出 ☰ → 点击弹出目录浮层：从当前预览的渲染结果里收集
// md-h1~h3 标题，点击条目滚动正文到对应标题。随文件切换重建/关闭。
let attachToc = [];  // [{id, lvl, text}]

function buildAttachToc(box) {
  attachToc = [];
  box.querySelectorAll(".md-h1, .md-h2, .md-h3").forEach((el, idx) => {
    const id = `att-toc-${idx}`;
    el.id = id;
    attachToc.push({ id, lvl: +el.tagName[1], text: el.textContent });
  });
  closeAttachToc();  // 换文件后旧目录浮层立即作废
}

function toggleAttachToc(name) {
  const pop = $("attach-toc-pop");
  if (!pop) return;
  if (!pop.classList.contains("hidden") && pop.dataset.name === name) {
    closeAttachToc();
    return;
  }
  pop.dataset.name = name;
  const list = pop.querySelector(".attach-toc-list");
  list.innerHTML = "";
  if (!attachToc.length) {
    list.innerHTML = '<div class="attach-toc-empty">这篇文档没有可导航的标题</div>';
  } else {
    for (const t of attachToc) {
      const row = document.createElement("div");
      row.className = "attach-toc-row";
      row.style.paddingLeft = (8 + (t.lvl - 1) * 14) + "px";
      row.textContent = t.text;
      row.addEventListener("click", (e) => {
        // stopPropagation 必须加：目录浮层挂在 body 上，点击会冒泡到 document
        // 的「点空白关浮窗」监听器——它看到目标不在 attach-pop 内，会把整个
        // 附件浮窗连带关掉（表现就是"点了目录浮窗没了、正文也没跳"）。
        e.stopPropagation();
        const box = $("attach-view").querySelector(".docs-view");
        const el = box && box.querySelector("#" + CSS.escape(t.id));
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "start" });
          el.classList.remove("toc-flash");
          void el.offsetWidth;  // 重启动画
          el.classList.add("toc-flash");
        }
        closeAttachToc();
      });
      list.appendChild(row);
    }
  }
  // 贴在列表项右侧：锚点是触发按钮所在 li
  const li = document.querySelector(`.attach-item[data-name="${CSS.escape(name)}"]`);
  if (li) {
    const r = li.getBoundingClientRect();
    const pr = $("attach-pop").getBoundingClientRect();
    pop.style.left = Math.min(r.right + 8, pr.right - 220) + "px";
    pop.style.top = Math.min(r.top, pr.bottom - 60) + "px";
  }
  pop.classList.remove("hidden");
}

function closeAttachToc() {
  const pop = $("attach-toc-pop");
  if (pop) pop.classList.add("hidden");
}

async function openAttachment(name) {
  if (!currentSession) return;
  const view = $("attach-view");
  if (!view) return;
  attachActive = name;
  document.querySelectorAll(".attach-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.name === name);
  });
  view.innerHTML = '<div class="attach-empty">加载中…</div>';
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}`
      + `/attachments?name=${encodeURIComponent(name)}`);
    view.innerHTML = "";
    if (data.kind === "archive") {
      view.appendChild(renderArchiveInfo(data));
    } else if (data.kind === "binary") {
      const d = document.createElement("div");
      d.className = "attach-empty";
      d.textContent = `《${data.name}》是二进制文件，无法在页面上预览。`
        + "附件已落盘，可用本地编辑器打开。";
      view.appendChild(d);
    } else if (/\.md$/i.test(data.name || "")) {
      // Markdown 文件：与文档面板同款渲染（renderMarkdown），不再裸显 # 记号。
      // 注意 truncated 只在超 2000 行时出现——渲染路径以完整内容为准，超长仍走纯文本。
      const head = document.createElement("div");
      head.className = "attach-file-head";
      head.textContent = `${data.name}　Markdown 渲染`;
      const box = document.createElement("div");
      box.className = "docs-view";  // 直接复用文档面板的排版样式（标题/列表/行距）
      box.style.padding = "4px 0";
      box.appendChild(renderMarkdown(data.content || "（空文件）"));
      view.append(head, box);
      buildAttachToc(box);  // 从渲染结果收集标题，目录入口随文件切换重建
    } else {
      // 纯文本预览：压缩 3 行及以上的连续空行为 1 个空行（常见于导出文档，
      // 原文段间 2~3 空行在等宽字体下显得非常松散）。只改显示，不动原文件。
      const head = document.createElement("div");
      head.className = "attach-file-head";
      head.textContent = `${data.name}　第 ${data.offset + 1}~${data.offset + data.lines} 行`
        + `（共 ${data.total_lines} 行）`;
      const pre = document.createElement("pre");
      pre.className = "attach-text mono";
      pre.textContent = (data.content || "（空文件）").replace(/\n{3,}/g, "\n\n");
      view.append(head, pre);
      if (data.truncated) {
        const more = document.createElement("div");
        more.className = "attach-more";
        more.textContent = "…内容过长，仅显示前 2000 行。完整文件请用本地编辑器打开。";
        view.appendChild(more);
      }
    }
  } catch (e) {
    view.innerHTML = `<div class="attach-empty">打开失败：${esc(e.message)}</div>`;
  }
}

// 压缩包：只列成员清单（与 agent 的 read_attachment 看到的一致）
function renderArchiveInfo(data) {
  const wrap = document.createElement("div");
  const head = document.createElement("div");
  head.className = "attach-file-head";
  head.textContent = `${data.name}　压缩包（${data.archive_kind || "未知格式"}），`
    + `含 ${data.file_count} 个成员`;
  wrap.appendChild(head);
  const ul = document.createElement("ul");
  ul.className = "attach-members";
  for (const m of (data.members || []).slice(0, 200)) {
    const li = document.createElement("li");
    li.textContent = m.note ? `${m.name}　${m.note}` : m.name;
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  if (data.file_count > 200) {
    const more = document.createElement("div");
    more.className = "attach-more";
    more.textContent = `…还有 ${data.file_count - 200} 个成员`;
    wrap.appendChild(more);
  }
  return wrap;
}

// ---------- 右侧文档栏 ----------
// 展示本会话 agent 生成的 Markdown 文档：列表 + 渲染。入口是工具栏的
// 📄 文档 chip；生成完成（doc_created 事件）时自动展开并打开新文档。
// 面板与主区【并列】（不是浮层），宽度由 CSS 变量 --docs-w 驱动，
// 展开/收起/拖拽都只改这个变量，主区 flex:1 自动跟着压缩或变宽。
function docsPanelEl() { return $("docs-panel"); }

// 文档栏宽度的取值区间与持久化：全局记忆（不按会话），下次打开保持。
const DOCS_W_MIN = 360;
const DOCS_W_MAX_RATIO = 0.88;   // 最宽不超过视口的 88%
const DOCS_W_KEY = "docsWidth";
const DOCS_LIST_KEY = "docsListCollapsed";

function docsMaxWidth() { return Math.round(window.innerWidth * DOCS_W_MAX_RATIO); }

function clampDocsWidth(w) {
  return Math.max(DOCS_W_MIN, Math.min(docsMaxWidth(), Math.round(w)));
}

// 把宽度写进 CSS 变量（面板与主区随之变化）
function applyDocsWidth(w) {
  const px = clampDocsWidth(w);
  document.documentElement.style.setProperty("--docs-w", px + "px");
  return px;
}

function restoreDocsWidth() {
  const saved = parseInt(localStorage.getItem(DOCS_W_KEY) || "", 10);
  applyDocsWidth(Number.isFinite(saved) ? saved : 620);
}

// 文档列表折叠态：只切换面板上的 class，CSS 负责宽度过渡
function restoreDocsListState() {
  const p = docsPanelEl();
  if (!p) return;
  const collapsed = localStorage.getItem(DOCS_LIST_KEY) === "1";
  p.classList.toggle("list-collapsed", collapsed);
  syncDocsToggleBtn(collapsed);   // 图标/title 与恢复的折叠态保持一致
}

// 让标题栏折叠按钮的图标与提示跟随折叠态
function syncDocsToggleBtn(collapsed) {
  const btn = $("docs-toggle");
  if (!btn) return;
  btn.textContent = collapsed ? "⇥" : "⇤";
  btn.title = collapsed ? "展开文档列表" : "收起文档列表";
}

function toggleDocsList() {
  const p = docsPanelEl();
  if (!p) return;
  const collapsed = p.classList.toggle("list-collapsed");
  localStorage.setItem(DOCS_LIST_KEY, collapsed ? "1" : "0");
  syncDocsToggleBtn(collapsed);
}

// 拖拽把手：按下后跟手改宽度，松开结束。拖拽期间禁用过渡（见 CSS）。
function bindDocsResizer() {
  const handle = $("docs-resizer");
  const p = docsPanelEl();
  if (!handle || !p) return;
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = p.getBoundingClientRect().width;
    document.body.classList.add("docs-resizing");
    const onMove = (ev) => {
      // 向左拖（clientX 变小）→ 变宽，所以用 startX - ev.clientX
      applyDocsWidth(startW + (startX - ev.clientX));
    };
    const onUp = () => {
      document.body.classList.remove("docs-resizing");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const cur = clampDocsWidth(p.getBoundingClientRect().width);
      localStorage.setItem(DOCS_W_KEY, String(cur));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
  // 视口变窄时把过宽的面板收进合法区间
  window.addEventListener("resize", () => {
    const cur = parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue("--docs-w"), 10);
    if (Number.isFinite(cur)) applyDocsWidth(cur);
  });
}

function toggleDocsPanel() {
  const p = docsPanelEl();
  if (!p) return;
  if (p.classList.contains("hidden")) {
    p.classList.remove("hidden");
    closeBrowserPanel();  // 与浏览器栏互斥（共用 --docs-w）
    loadDocsList();
  } else {
    p.classList.add("hidden");
  }
}

function closeDocsPanel() {
  const p = docsPanelEl();
  if (p) p.classList.add("hidden");
}

// 切会话/新建任务时把文档栏彻底复位：清空列表与内容区并收起面板。
// 为什么必须显式收起：loadDocsList 在新会话无文档时会清空内容区，
// 但不会把面板藏起来——若上一个会话正开着文档栏，切过来就会看到
// 一个「已展开却是空白」的文档栏（像凭空弹出一个空 tab）。
function resetDocsPanel() {
  const p = docsPanelEl();
  if (p) p.classList.add("hidden");
  const list = $("docs-list");
  if (list) list.innerHTML = "";
  const view = $("docs-view");
  if (view) view.innerHTML = "";
  const countEl = $("docs-count");
  if (countEl) countEl.textContent = "0";
}

// 拉取当前会话文档列表，刷新列表与计数。会话为空（新任务态）时清空。
async function loadDocsList() {
  const list = $("docs-list");
  const countEl = $("docs-count");
  if (!list) return;
  if (!currentSession) {
    list.innerHTML = "";
    if (countEl) countEl.textContent = "0";
    renderDocsEmpty("本会话还没有文档，试试让 agent 生成一份。");
    return;
  }
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}/docs`);
    const docs = data.docs || [];
    if (countEl) countEl.textContent = String(docs.length);
    list.innerHTML = "";
    if (!docs.length) {
      renderDocsEmpty("本会话还没有文档，试试让 agent 生成一份。");
      return;
    }
    for (const d of docs) {
      const li = document.createElement("li");
      li.className = "docs-item";
      li.dataset.name = d.name;
      const name = document.createElement("span");
      name.textContent = d.name;
      const meta = document.createElement("span");
      meta.className = "docs-meta";
      meta.textContent = `${fmtBytes(d.bytes)} · ${fmtDocTime(d.mtime)}`;
      li.append(name, meta);
      li.addEventListener("click", () => openDoc(d.name));
      li.appendChild(makeDeleteBtn("docs-del-btn", d.name, deleteDoc));
      list.appendChild(li);
    }
  } catch (e) {
    renderDocsEmpty("文档列表加载失败：" + e.message);
  }
}

// 删除一份会话文档：调 DELETE 接口 → 刷新列表；删的是右侧正展示的那份就清空视图。
// openDoc 会给列表项打 .active 且 data-name 即文件名，据此判断"开的是不是它"。
async function deleteDoc(name) {
  await api(`/api/sessions/${encodeURIComponent(currentSession)}/docs?name=${encodeURIComponent(name)}`,
    { method: "DELETE" });
  // 先判断再刷新：loadDocsList 会重建列表，被删项在新列表里已不存在
  const wasOpen = document.querySelector(`.docs-item[data-name="${CSS.escape(name)}"].active`);
  await loadDocsList();
  if (wasOpen) $("docs-view").innerHTML = '<div class="docs-empty">文档已删除。</div>';
}

function renderDocsEmpty(msg) {
  const view = $("docs-view");
  const list = $("docs-list");
  if (list) {
    const li = document.createElement("li");
    li.className = "docs-empty";
    li.textContent = msg;
    list.appendChild(li);
  }
  if (view) view.innerHTML = "";
}

function fmtDocTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 打开一份文档：拉原文 → 渲染到右侧视图，并高亮列表项。
async function openDoc(name) {
  if (!currentSession) return;
  const view = $("docs-view");
  if (!view) return;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(currentSession)}`
      + `/docs/content?name=${encodeURIComponent(name)}`);
    view.replaceChildren(renderMarkdown(data.content || ""));
  } catch (e) {
    const err = document.createElement("div");
    err.className = "docs-empty";
    err.textContent = "文档打开失败：" + e.message;
    view.replaceChildren(err);
  }
  document.querySelectorAll(".docs-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.name === name);
  });
}

// doc_created 事件：刷新列表、展开面板、打开新文档。
function onDocCreated(name) {
  const p = docsPanelEl();
  if (p) p.classList.remove("hidden");
  loadDocsList().then(() => { if (name) openDoc(name); });
  if (name) toast(`已生成文档《${name}》`);
}

// ---------- 🌐 浏览器栏：agent 内置浏览器的实时截图流 ----------
// 与文档栏同一时刻只开一个：browser_shot 到达时收起文档栏（反之亦然）。
// 截图本体走 /api/sessions/<sid>/browser/shot 接口按需拉取，事件里只带 URL。

function browserPanelEl() { return $("browser-panel"); }

// browser_shot 事件：展开浏览器栏、显示最新截图。
function onBrowserShot(url, note, shot) {
  const chip = $("browser-chip");
  if (chip) chip.classList.remove("hidden");
  const p = browserPanelEl();
  if (!p) return;
  if (!p.classList.contains("hidden")) syncBrowser(url, note, shot);
  else {
    p.classList.remove("hidden");
    closeDocsPanel();  // 两个面板共用 --docs-w：只保留一个，避免互相挤压
    syncBrowser(url, note, shot);
  }
}

// 把一条截图信息渲染进浏览器栏（追加，保留历史画面可回看）
function syncBrowser(url, note, shot) {
  const urlEl = $("browser-url"), noteEl = $("browser-note"), view = $("browser-view");
  if (!view) return;
  if (urlEl) { urlEl.textContent = url || "—"; urlEl.title = url || ""; }
  if (noteEl) noteEl.textContent = note || "";
  const img = document.createElement("img");
  // <img> 是浏览器原生请求，带不了 Authorization 头——token 走查询参数
  // （与 SSE events 端点同一先例），服务端 _require_auth 对该端点放行。
  // t= 破缓存：同一 n 的截图内容不会变，但保底避免历史帧被内存缓存干扰。
  img.src = shot + (shot.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(authToken) + "&t=" + Date.now();
  img.alt = note || url || "浏览器截图";
  view.appendChild(img);
  view.scrollTop = view.scrollHeight;
}

function closeBrowserPanel() {
  const p = browserPanelEl();
  if (p) p.classList.add("hidden");
}

// 切会话/新建任务时复位浏览器栏：收起 + 清空画面，chip 藏回（新会话还没有浏览器活动）
function resetBrowserPanel() {
  closeBrowserPanel();
  const view = $("browser-view");
  if (view) view.innerHTML = "";
  const urlEl = $("browser-url"), noteEl = $("browser-note"), chip = $("browser-chip");
  if (urlEl) urlEl.textContent = "—";
  if (noteEl) noteEl.textContent = "";
  if (chip) chip.classList.add("hidden");
}
