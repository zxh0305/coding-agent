// 冒烟验证②：快照卡与 SSE 补发实时卡的互斥（tagLiveTrace 打标 + ensureTrace 接管）
// 用法：node backend/manual/smoke_trace_takeover.js
// 做法：从 frontend/app.js 源码里按花括号配平截取这两个函数体（测的是本体不是复刻），
// 把模块级 let traceEl 的读写替换为注入的 ref，chatEl/document 走最小 DOM stub。
const fs = require("fs");
const path = require("path");

const el = () => {
  const n = {
    children: [], style: {}, dataset: {}, open: false, _attrs: {},
    classList: {
      _s: new Set(),
      add(...cs) { cs.forEach((c) => this._s.add(c)); },
      remove(...cs) { cs.forEach((c) => this._s.delete(c)); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
    querySelector(sel) {
      const classes = sel.replace(/^\[|\]$/g, "").startsWith("data-")
        ? null : sel.split(".").filter(Boolean);
      const dataKey = (sel.match(/\[data-([\w-]+)\]/) || [])[1];
      const match = (x) => {
        if (classes) return classes.every((c) => x.classList.contains(c));
        if (dataKey) return x.dataset[dataKey.replace(/-(\w)/g, (_, m) => m.toUpperCase())] != null;
        return false;
      };
      const walk = (x) => {
        for (const ch of x.children) { if (match(ch)) return ch; const r = walk(ch); if (r) return r; }
        return null;
      };
      return walk(n);
    },
    querySelectorAll() { return []; },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k]; },
    removeAttribute(k) {
      delete this._attrs[k];
      if (k.startsWith("data-")) {  // 与 dataset 互通（ensureTrace 摘标记走这里）
        delete this.dataset[k.slice(5).replace(/-(\w)/g, (_, m) => m.toUpperCase())];
      }
    },
    addEventListener() {},
    isConnected: true,
  };
  // className 赋值 ↔ classList 互通（ensureTrace 新建分支写 className）
  Object.defineProperty(n, "className", {
    set(v) { v.split(/\s+/).filter(Boolean).forEach((c) => n.classList.add(c)); },
    get() { return [...n.classList._s].join(" "); },
  });
  // dataset.set livetrace ↔ data-livetrace 属性互通（removeAttribute 后 dataset 也清）
  const ds = n.dataset;
  Object.defineProperty(n, "data-livetrace", {
    get() { return ds.livetrace; }, set(v) { ds.livetrace = v; },
  });
  return n;
};
global.document = {
  createElement: el,
  createDocumentFragment: () => { const f = el(); f.nodeType = 11; return f; },
};

// —— 花括号配平截取函数体（非贪婪正则会吃掉嵌套块，不能用）——
const src = fs.readFileSync(path.join(__dirname, "../../frontend/app.js"), "utf8");
function grabFn(name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数 " + name);
  const bodyOpen = src.indexOf("{", start);
  let depth = 0;
  for (let i = bodyOpen; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (!depth) return src.slice(start, i + 1); }
  }
  throw new Error("括号不配平：" + name);
}

const traceElRef = { get: () => store.v, set: (x) => { store.v = x; } };
const store = { v: null };
const chatEl = el();

const tagSrc = grabFn("tagLiveTrace");
// traceEl 读写分开替换：读 → ref.get()，写 → ref.set(x)。
// 两类语句的原文形态：
//   if (traceEl) return;                      → if (traceElRef.get()) return;
//   traceEl = existing;                       → traceElRef.set(existing);
//   traceEl = document.createElement(...);    → const __t = document.createElement(...); traceElRef.set(__t);
//   traceEl.xxx（属性访问，新建分支 4 处）     → const __t.xxx
const ensSrc = grabFn("ensureTrace")
  .replace("if (traceEl) return;", "if (traceElRef.get()) return;")
  .replace("traceEl = existing;", "traceElRef.set(existing); return traceElRef.get();")
  .replace('traceEl = document.createElement("details");',
           'var __t = document.createElement("details"); traceElRef.set(__t);')
  .replace(/\btraceEl\b/g, "__t");   // 兜底：剩余的属性访问/appendChild(traceEl) 等裸引用

const api = new Function("chatEl", "document", "traceElRef", `
  ${tagSrc}
  ${ensSrc}
  return {
    tag: (n, m) => tagLiveTrace(n, m),
    ensure: () => { ensureTrace(); return traceElRef.get(); },
  };
`)(chatEl, global.document, traceElRef);

let fail = 0;
const check = (name, cond) => { console.log((cond ? "  ✅" : "  ❌") + " " + name); if (!cond) fail++; };

// —— 场景：切回会话，历史渲染已画出快照卡（卡已挂在时间线 #chat 上）——
const snapCard = el();
snapCard.classList.add("trace", "running");
chatEl.appendChild(snapCard);          // 历史渲染的真实落点
const frag = global.document.createDocumentFragment();
frag.append(el(), snapCard);   // user 气泡 + 快照卡（blocks 路径返回形状）

api.tag(frag, { mid: "u1" });
check("快照卡被打上 data-livetrace 标记", snapCard.dataset.livetrace === "u1");

// SSE 补发 turn_start → ensureTrace 第一次调用：应接管快照卡而非新建
const taken = api.ensure();
check("ensureTrace 接管已有快照卡（引用相同）", taken === snapCard);
check("接管后标记被摘除（防二次接管）", !("livetrace" in snapCard.dataset));
const traceCountBefore = chatEl.children.filter((c) => c.classList.contains("trace")).length;
check("时间线上没有新建卡（trace 卡数量不变）",
      chatEl.children.filter((c) => c.classList.contains("trace")).length === traceCountBefore
      && chatEl.children.filter((c) => c.classList.contains("trace")).every((c) => c === snapCard));

// traceEl 已有引用时幂等
const again = api.ensure();
check("traceEl 已存在时幂等返回", again === snapCard);

// 无标记卡的新回合：走正常新建路径
store.v = null;
const fresh = api.ensure();
check("无标记时新建（正常实时路径）", !!fresh && fresh.classList.contains("trace") && fresh.classList.contains("running"));
check("新建卡入时间线", chatEl.children.includes(fresh));

console.log(fail ? `\n失败 ${fail} 项` : "\n全部通过");
process.exit(fail ? 1 : 0);
