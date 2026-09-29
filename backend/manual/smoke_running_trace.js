// 冒烟验证：进行中回合快照（_runningTrace）在两条渲染路径下都能画出 process 块
// 用法：node backend/manual/smoke_running_trace.js
const path = require("path");
const fs = require("fs");

// 最小 DOM stub（render_blocks 只用 createElement/createDocumentFragment）
const el = () => {
  const n = {
    children: [], style: {}, dataset: {}, classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
    querySelector() { return el(); }, querySelectorAll() { return []; },
    setAttribute() {}, addEventListener() {}, replaceChildren(...cs) { this.children = cs; },
    isConnected: true,
  };
  return n;
};
global.document = {
  createElement: el,
  createDocumentFragment: () => {
    const f = el();
    Object.defineProperty(f, "nodeType", { value: 11 });
    return f;
  },
};

// 加载两个模块（frontend 下是 UMD 风格：优先 module.exports，其次 window）
const src = fs.readFileSync(path.join(__dirname, "../../frontend/blocks.js"), "utf8");
const window = {};
const mod = { exports: {} };
new Function("module", "window", "document", src)(mod, window, global.document);
const Blocks = mod.exports || window.CodingAgentBlocks;

const src2 = fs.readFileSync(path.join(__dirname, "../../frontend/render_blocks.js"), "utf8");
const mod2 = { exports: {} };
new Function("module", "window", "document", src2)(mod2, window, global.document);
const RB = mod2.exports || window.CodingAgentRenderBlocks;

const deps = {
  doc: global.document,
  buildBubble: (role, text) => { const b = el(); b.className = "bubble " + role; b.textContent = text; return b; },
  railTag: (n) => n,
  makeToolCallLine: (name, args) => { const c = el(); c.className = "tool-line"; c.textContent = name + " " + (args || ""); return c; },
  makeToolResultLine: (name, result) => { const r = el(); r.className = "tool-result"; r.textContent = (result || "").slice(0, 50); return r; },
  buildUserBubble: (role, text) => { const b = el(); b.className = "bubble " + role; b.textContent = text; return b; },
  decorateWriteCard: (c) => c,
  metaText: () => { const m = el(); m.className = "meta"; return m; },
  makeRetryButton: () => el(),
  compactCard: () => el(),
  fmtElapsed: (s) => Math.round(s) + "s",
};

// —— 构造：带快照的 user 消息（当前回合的真实形状）——
const userMsg = {
  role: "user", mid: "u1",
  content: [{ type: "text", text: "看一下这个问题" }, { type: "image_url", image_url: { url: "data:image/png;base64,x" } }],
  _runningTrace: [
    { type: "round", round: 1 },
    { type: "reasoning", text: "先看截图内容" },
    { type: "tool_call", name: "analyze_image", arguments: '{"question":"图里有什么"}' },
    { type: "tool_result", name: "analyze_image", result: '{"ok":true}' },
    { type: "tool_call", name: "run_bash", arguments: '{"command":"git log"}' },  // 无 result：应保持 running
  ],
  _runningStartedAt: Date.now() / 1000 - 42,
};

let fail = 0;
const check = (name, cond) => { console.log((cond ? "  ✅" : "  ❌") + " " + name); if (!cond) fail++; };

// 路径1：blocksFromHistory 的 user 分支
{
  const blocks = Blocks.blocksFromHistory([userMsg]);
  const user = blocks.find((b) => b.kind === "user");
  const live = blocks.find((b) => b.kind === "process");
  check("user 分支：用户气泡存在", !!user);
  check("user 分支：running process 块存在", !!live);
  if (live) {
    check("process 块 running=true", live.running === true);
    check("process 块 startedAt 透传", typeof live.startedAt === "number");
    check("末位工具保持 running 状态", live.items[live.items.length - 1].status === "running");
    check("含 reasoning 条目", live.items.some((i) => i.kind === "reasoning"));
    const elp = RB.createRenderer(deps).renderBlock(live);
    check("renderBlock 产出 trace DOM", !!elp && elp.className.indexOf("running") >= 0);
  }
}

// 路径2：runningTraceBlock 直取（app.js artifact 路径用）
{
  const live = Blocks.runningTraceBlock(userMsg);
  check("artifact 路径：runningTraceBlock 返回块", !!live && live.kind === "process");
  const el2 = RB.createRenderer(deps).renderBlock(live);
  check("artifact 路径：renderBlock 产出 DOM", !!el2);
  check("无快照消息返回 null", Blocks.runningTraceBlock({ role: "user", content: "hi" }) === null);
}

// 回归：无快照的普通历史不受影响
{
  const blocks = Blocks.blocksFromHistory([
    { role: "user", content: "你好" },
    { role: "assistant", content: "在的", trace: [{ type: "round", round: 1 }], stats: { elapsed_s: 1.2 } },
  ]);
  check("回归：普通历史 user 无多余 process 块", !blocks.some((b) => b.kind === "process" && b.running));
  check("回归：assistant 正常出块", blocks.some((b) => b.kind === "answer"));
}

console.log(fail ? `\n失败 ${fail} 项` : "\n全部通过");
process.exit(fail ? 1 : 0);
