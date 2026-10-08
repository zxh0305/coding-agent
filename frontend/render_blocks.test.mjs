/*
 * frontend/render_blocks.js 的单元测试（Node 原生 test runner）
 * 运行：node --test frontend/render_blocks.test.mjs
 *
 * 用极简的假 DOM（只实现用到的 API）验证渲染器的节点结构，
 * 不依赖浏览器。重点：块 → 节点树的映射、过程块折叠、状态处理。
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { createRenderer } = require("./render_blocks.js");

// ---------- 极简假 DOM ----------

class FakeEl {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.dataset = {};
    this._cls = new Set();
    this.classList = {
      add: (...cs) => cs.forEach((c) => this._cls.add(c)),
      contains: (c) => this._cls.has(c) || this.className.split(" ").includes(c),
      remove: (c) => this._cls.delete(c),
    };
  }
  appendChild(n) {
    this.children.push(n);
    if (n && n._isFragment) {
      // fragment 展开（模拟真实 DOM 行为）
      this.children.pop();
      this.children.push(...n.children);
    }
    return n;
  }
  append(...ns) {
    ns.forEach((n) => this.appendChild(n));
    return this;
  }
  replaceChildren(...ns) {
    this.children = [];
    ns.forEach((n) => this.appendChild(n));
  }
  querySelector(sel) {
    if (sel === "summary") return this.children.find((c) => c.tagName === "SUMMARY") || null;
    return null;
  }
}

const fakeDoc = {
  createElement: (t) => new FakeEl(t),
  createDocumentFragment: () => {
    const f = new FakeEl("#fragment");
    f._isFragment = true;
    return f;
  },
};

// ---------- 假 deps：记录调用，产出可断言的节点 ----------

function makeDeps() {
  const calls = [];
  return {
    calls,
    doc: fakeDoc,
    buildBubble: (cls, text) => {
      const el = new FakeEl("div");
      el.className = "bubble " + cls;
      el.textContent = text;
      return el;
    },
    buildUserBubble: (text, atts) => {
      const el = new FakeEl("div");
      el.className = "bubble user";
      el.textContent = text;
      el.atts = atts;
      return el;
    },
    railTag: (node, mid, role) => {
      if (mid) node.dataset.mid = mid;
      if (role) node.dataset.role = role;
      return node;
    },
    makeToolCallLine: (name, args) => {
      calls.push(["call", name, args]);
      const el = new FakeEl("details");
      el.className = name === "write_file" || name === "apply_patch" ? "tl write card" : "tl";
      return el;
    },
    makeToolResultLine: (name, result) => {
      calls.push(["result", name, result]);
      const el = new FakeEl("details");
      el.className = "tl result";
      return el;
    },
    decorateWriteCard: (el, result) => calls.push(["decorate", result]),
    metaText: (elapsed, usage) => `耗时 ${elapsed}s`,
    compactCard: (summary) => {
      const el = new FakeEl("details");
      el.className = "compact-divider";
      el.textContent = summary;
      return el;
    },
    fmtElapsed: (s) => `${s}s`,
  };
}

// ---------- 测试 ----------

test("渲染：用户块带 mid/role 锚点（导航条定位用）", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([{ kind: "user", text: "你好", atts: [], mid: "u1" }]);
  const node = frag.children[0];
  assert.equal(node.className, "bubble user");
  assert.equal(node.dataset.mid, "u1");
  assert.equal(node.dataset.role, "user");
});

test("渲染：回答块走 assistant 气泡，流式时带 streaming 类", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([{ kind: "answer", text: "答案", mid: "a1", streaming: true }]);
  const node = frag.children[0];
  assert.equal(node.className, "bubble assistant");
  assert.ok(node.classList.contains("streaming"));
  assert.equal(node.dataset.role, "assistant");
});

test("渲染：过程块 details.trace，定稿折叠、进行中（running）展开", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([
    { kind: "process", steps: 2, elapsed: 3.5, items: [
      { kind: "note", text: "🧠 思考 · 第 1 轮", roundHead: true },
      { kind: "tool", name: "read_file", arguments: "{}", result: '{"ok":true}', status: "ok" },
    ] },
  ]);
  const d = frag.children[0];
  assert.equal(d.tagName, "DETAILS");
  assert.ok(d.classList.contains("trace"));
  assert.equal(d.open, false); // 已定稿：默认折叠
  assert.equal(d.querySelector("summary").textContent, "已工作 3.5s · 2 步");

  // 进行中回合（切会话/刷新回来的 running 快照）：默认展开，用户可手动收起
  const live = r.renderBlocks([
    { kind: "process", running: true, startedAt: Date.now() / 1000,
      steps: 1, elapsed: null, items: [] },
  ]).children[0];
  assert.equal(live.open, true);
});

test("渲染：工具调用与结果配对成两行，写入卡回填徽章", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([
    { kind: "process", steps: 1, elapsed: null, items: [
      { kind: "tool", name: "write_file", arguments: '{"path":"a.py"}', result: '{"ok":true,"lines":3}', status: "ok" },
    ] },
  ]);
  const d = frag.children[0];
  // summary + 修改区（写入类工具单独成区，夹在过程窗与说明卡之间）
  assert.equal(d.children.length, 2);
  const win = d.children[1];
  assert.equal(win.className, "edit-box");
  assert.equal(win.children.length, 2);  // 调用卡 + 结果行，区内配对
  assert.ok(win.children[0].classList.contains("card"));
  assert.ok(deps.calls.some((c) => c[0] === "decorate"), "写入卡应回填徽章");
  assert.ok(deps.calls.some((c) => c[0] === "result"), "应有结果行");
});

test("渲染：running / waiting 的工具不画结果行（还没有结果）", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  for (const status of ["running", "waiting"]) {
    deps.calls.length = 0;
    const frag = r.renderBlocks([
      { kind: "process", steps: 1, elapsed: null, items: [
        { kind: "tool", name: "grep", arguments: "{}", result: null, status },
      ] },
    ]);
    const d = frag.children[0];
    assert.equal(d.children.length, 2, status + " 只应有 summary + 过程窗");
    assert.equal(d.children[1].className, "proc-line");
    assert.equal(d.children[1].children.length, 1, status + " 窗内只有调用行");
    assert.ok(!deps.calls.some((c) => c[0] === "result"), status + " 不该有结果行");
  }
});

test("渲染：思考流与过程说明用不同类名（不冒充）", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([
    { kind: "process", steps: 0, elapsed: null, items: [
      { kind: "reasoning", text: "先看目录" },
      { kind: "note", text: "我打算这样做", demoted: true },
    ] },
  ]);
  const d = frag.children[0];
  const win = d.children[1];   // 过程监视窗（思考+工具共居一窗）
  const noteBox = d.children[2];
  assert.equal(win.className, "proc-line");
  assert.equal(noteBox.className, "note-box demoted");
  // 思考在窗内的正文段里，说明在卡的子段里——两类文字绝不共用一个容器
  assert.equal(win.children[0].className, "think-seg-body");
  assert.equal(win.children[0].textContent, "先看目录");
  assert.equal(noteBox.children[0].className, "process-text");
  assert.equal(noteBox.children[0].textContent, "我打算这样做");
});

test("渲染：整回合思考归一窗、说明归一卡，轮次标题变窗内分段头", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([
    { kind: "process", running: true, steps: 1, elapsed: null, items: [
      { kind: "note", text: "🧠 思考 · 第 1 轮", roundHead: true },
      { kind: "reasoning", text: "先看目录" },
      { kind: "note", text: "我打算这样做", demoted: true },
      { kind: "tool", name: "grep", arguments: "{}", result: null, status: "running" },
      { kind: "note", text: "🧠 思考 · 第 2 轮", roundHead: true },
      { kind: "reasoning", text: "再看结果", live: true },
      { kind: "note", text: "还是不对", demoted: true },
    ] },
  ]);
  const d = frag.children[0];
  // 顶层顺序：summary → 过程窗（思考+工具共居） → 说明卡（沉底）
  assert.deepEqual(d.children.slice(1).map((c) => c.className),
                   ["proc-line", "note-box demoted"]);
  // 两轮思考进同一个窗，轮次标题降为窗内分段头；快照末段带 live 光标；
  // running 工具行也进窗、按真实时序插在两轮思考之间
  const win = d.children[1];
  assert.deepEqual(win.children.map((c) => c.className),
                   ["think-seg-head", "think-seg-body", "tl",
                    "think-seg-head", "think-seg-body live"]);
  assert.equal(win.children[0].textContent, "🧠 思考 · 第 1 轮");
  assert.equal(win.children[1].textContent, "先看目录");
  assert.equal(win.children[3].textContent, "🧠 思考 · 第 2 轮");
  assert.equal(win.children[4].textContent, "再看结果");
  // 两轮说明进同一张卡
  const nb = d.children[2];
  assert.deepEqual(nb.children.map((c) => c.textContent), ["我打算这样做", "还是不对"]);
});

test("渲染：没跟思考流的轮次标题留在时间线（非思考模型回合）", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([
    { kind: "process", steps: 1, elapsed: null, items: [
      { kind: "note", text: "🧠 思考 · 第 1 轮", roundHead: true },
      { kind: "tool", name: "grep", arguments: "{}", result: '{"ok":true}', status: "ok" },
    ] },
  ]);
  const d = frag.children[0];
  // 没有任何 reasoning：轮次标题与工具条目共同收进过程窗（proc-line）
  assert.deepEqual(d.children.slice(1).map((c) => c.className),
                   ["proc-line"]);
  assert.deepEqual(d.children[1].children.map((c) => c.className || c.tagName),
                   ["trace-line", "tl", "tl result"]);
});

test("渲染：压缩卡与统计行", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([
    { kind: "compact", summary: "早期摘要" },
    { kind: "meta", elapsed: 1.2, usage: { prompt_tokens: 10 } },
  ]);
  assert.equal(frag.children[0].className, "compact-divider");
  assert.equal(frag.children[1].className, "meta");
  assert.equal(frag.children[1].textContent, "耗时 1.2s");
});

test("渲染：错误块用 error 气泡", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderBlocks([{ kind: "error", text: "连接中断" }]);
  assert.equal(frag.children[0].className, "bubble error");
});

test("渲染：retryable 错误块带重试按钮，非 retryable 不带", () => {
  const deps = makeDeps();
  deps.makeRetryButton = () => {
    const b = new FakeEl("button");
    b.className = "retry-btn";
    b.textContent = "↻ 重试上一条";
    return b;
  };
  const r = createRenderer(deps);
  const withBtn = r.renderBlocks([{ kind: "error", text: "限流", retryable: true }]);
  // retryable 时错误块被包在一层 div holder 里：[div[气泡, 重试按钮]]
  assert.equal(withBtn.children[0].tagName, "DIV");
  assert.equal(withBtn.children[0].children[0].className, "bubble error");
  assert.equal(withBtn.children[0].children[1].className, "retry-btn");
  const noBtn = r.renderBlocks([{ kind: "error", text: "参数错误" }]);
  assert.equal(noBtn.children[0].className, "bubble error");
  assert.equal(noBtn.children.length, 1);
});

test("渲染：空数组与空输入不崩", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  assert.equal(r.renderBlocks([]).children.length, 0);
  assert.equal(r.renderBlocks(null).children.length, 0);
});

test("渲染：spawn_subagent 工具块的 subtasks 渲染成嵌套折叠卡", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const item = {
    kind: "tool", name: "spawn_subagent", arguments: '{"tasks":["A","B"]}',
    result: '{"ok":true,"results":[]}', status: "ok",
    subtasks: [
      { parent: "pA", index: 0, task: "任务A", items: [
        { kind: "note", roundHead: true, text: "🧠 第 1 轮" },
        { kind: "tool", name: "read_file", arguments: "{}", result: '{"ok":true,"result":"x"}', status: "ok" },
        { kind: "note", text: "✅ 完成 · 1 轮" },
      ]},
      { parent: "pB", index: 1, task: "任务B", items: [
        { kind: "note", text: "❌ boom" },
      ]},
    ],
  };
  const frag = r.renderProcessItem(item, false);
  const wrap = frag.children.find((n) => n.className === "subagent-cards");
  assert.ok(wrap, "应有嵌套容器");
  assert.equal(wrap.children.length, 2);
  const cardA = wrap.children[0];
  assert.equal(cardA.tagName, "DETAILS");
  assert.equal(cardA.className, "trace-nested");
  assert.equal(cardA.children[0].textContent, "🔍 子任务 1：任务A");
  // 内部条目复用同一渲染器：round 标题 + 工具行 + done 注记 → 归组进一个过程窗
  assert.deepEqual(cardA.children.slice(1).map((c) => c.className || c.tagName),
                   ["proc-line"]);
  const winA = cardA.children[1];
  assert.deepEqual(winA.children.map((c) => c.className || c.tagName),
                   ["trace-line", "tl", "tl result", "process-text"]);
  assert.equal(wrap.children[1].children[0].textContent, "🔍 子任务 2：任务B");
  // 调用行在前、容器居中、结果行在后
  assert.deepEqual(frag.children.map((c) => c.className || c.tagName),
                   ["tl", "subagent-cards", "tl result"]);
});

test("渲染：普通工具块没有 subtasks 不产生容器", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  const frag = r.renderProcessItem({
    kind: "tool", name: "read_file", arguments: "{}",
    result: '{"ok":true}', status: "ok",
  }, false);
  assert.equal(frag.children.find((n) => n.className === "subagent-cards"), undefined);
});
