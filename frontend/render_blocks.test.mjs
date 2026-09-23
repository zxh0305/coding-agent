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

test("渲染：过程块是折叠的 details.trace，摘要含步数", () => {
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
  assert.equal(d.open, false); // 默认折叠
  assert.equal(d.querySelector("summary").textContent, "已工作 3.5s · 2 步");
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
  // summary + 调用卡 + 结果行
  assert.equal(d.children.length, 3);
  assert.ok(d.children[1].classList.contains("card"));
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
    assert.equal(d.children.length, 2, status + " 只应有 summary + 调用行");
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
  const think = d.children[1];
  const note = d.children[2];
  assert.equal(think.className, "think-line");
  assert.equal(note.className, "process-text demoted");
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

test("渲染：空数组与空输入不崩", () => {
  const deps = makeDeps();
  const r = createRenderer(deps);
  assert.equal(r.renderBlocks([]).children.length, 0);
  assert.equal(r.renderBlocks(null).children.length, 0);
});
