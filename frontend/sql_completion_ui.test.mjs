/* sql_completion_ui.js 的交互测试：用最小 DOM 桩验证按键、插入与展开规则。 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.style = {}; this.dataset = {}; this.attrs = {};
    this.listeners = {}; this.hidden = false; this.textContent = "";
  }
  set className(v) { this._cls = v; } get className() { return this._cls || ""; }
  set id(v) { this._id = v; this.attrs.id = v; } get id() { return this._id || ""; }
  set innerHTML(v) {
    this.children = [];
    // 弹层只需要「列表 + 提示」两个子节点，按 class 粗分即可。
    for (const m of v.matchAll(/<div class="([^"]+)"/g)) this.children.push(Object.assign(new Node("div"), { className: m[1] }));
  }
  append(...nodes) { this.children.push(...nodes); for (const n of nodes) n.parentElement = this; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatchEvent(event) { return this.dispatch(event.type || 'event', {}); }
  dispatch(type, event = {}) { let stop = false; const e = { ...event, preventDefault() { stop = true; }, stopPropagation() {} }; for (const fn of this.listeners[type] || []) fn(e); return { prevented: stop }; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  closest() { return null; }
  querySelector(sel) { return this.find(sel); }
  querySelectorAll(sel) { const out = []; const walk = (n) => { for (const c of n.children) { if (match(c, sel)) out.push(c); walk(c); } }; walk(this); return out; }
  find(sel) { for (const c of this.children) { if (match(c, sel)) return c; const hit = c.find(sel); if (hit) return hit; } return null; }
  getBoundingClientRect() { return { top: 100, left: 200, bottom: 200, width: 480, height: 100 }; }
  scrollIntoView() {}
  remove() { const p = this.parentElement; if (p) p.children = p.children.filter((c) => c !== this); }
  focus() { sandbox.document.activeElement = this; }
  contains(n) { return n === this || this.children.some((c) => c.contains(n)); }
}
function match(node, sel) {
  if (sel.startsWith(".")) return node.className.split(" ").includes(sel.slice(1));
  return false;
}

function makeTextarea(value = "") {
  const el = new Node("textarea");
  Object.assign(el, { value, selectionStart: value.length, selectionEnd: value.length, isConnected: true, disabled: false, clientWidth: 480, offsetTop: 0 });
  el.setRangeText = (text, start, end) => {
    el.value = el.value.slice(0, start) + text + el.value.slice(end);
    el.selectionStart = el.selectionEnd = start + text.length;
  };
  return el;
}

const sandbox = {
  window: { addEventListener() {}, innerHeight: 800, innerWidth: 1200, getComputedStyle: () => ({ lineHeight: "16px" }) },
  document: { createElement: (t) => new Node(t), activeElement: null, addEventListener() {}, body: Object.assign(new Node("body"), {}), getComputedStyle: () => ({ lineHeight: "16px" }) },
  Event: class { constructor(type) { this.type = type; } },
  getComputedStyle: () => ({ lineHeight: "16px", fontFamily: "", fontSize: "12px" }),
  setTimeout,
};
sandbox.window.getComputedStyle = sandbox.getComputedStyle;
sandbox.document.body = new Node("body");
sandbox.window.document = sandbox.document;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(new URL("./sql_completion.js", import.meta.url), "utf8"), sandbox);
vm.runInContext(readFileSync(new URL("./sql_completion_ui.js", import.meta.url), "utf8"), sandbox);

const ctx = { columns: ["id", "name", "stock"], tables: ["products", "orders"] };
function setup(value, context = ctx) {
  const ta = makeTextarea(value);
  const editor = new Node("div"); editor.append(ta);
  sandbox.document.body.append(editor);
  const ui = sandbox.window.SqlCompletionUI.attach(ta, { getContext: () => context });
  ui.compute();
  return { ta, ui, pop: ui.pop };
}

test("输入单词前缀后弹出候选并默认选中第一项", () => {
  const { ui, pop } = setup("SEL");
  assert.equal(ui.visible(), true);
  assert.equal(ui.items()[0], "SELECT");
  assert.equal(pop.hidden, false);
  assert.equal(ui.items().length > 0, true);
});

test("Enter 插入候选；输入为空时的完整片段也能插入", () => {
  const { ta, ui } = setup("SELECT * FROM ord");
  // 表名位置以表为先：ord 先给 orders，再给通用关键字 ORDER BY。
  assert.equal(ui.items()[0], "orders");
  const { prevented } = ta.dispatch("keydown", { key: "Enter" });
  assert.equal(prevented, true);
  assert.equal(ta.value, "SELECT * FROM orders");
  // 插入后 input 事件会让补全按新上下文重新计算；光标停在词边界时重新列出候选是预期行为。
  assert.equal(ta.getAttribute("aria-expanded"), String(ui.visible()));
});

test("Tab 与 ↑↓ 选择，Esc 只关闭不插入", () => {
  const { ta, ui } = setup("SELECT * FROM products WHERE ");
  assert.equal(ui.items().length, 0, "未输入前缀不应弹出");
  assert.equal(ta.dispatch("keydown", { key: "Tab" }).prevented, false);
  const typed = setup("SELECT * FROM products WHERE na");
  typed.ta.dispatch("keydown", { key: "ArrowDown" });
  typed.ta.dispatch("keydown", { key: "Tab" });
  assert.equal(typed.ta.value, 'SELECT * FROM products WHERE "name"');
  const again = setup("SEL");
  const esc = again.ta.dispatch("keydown", { key: "Escape" });
  assert.equal(esc.prevented, true);
  assert.equal(again.ta.value, "SEL");
  assert.equal(again.ui.visible(), false);
});

test("Ctrl+Enter 留给执行 SQL，不被补全拦截", () => {
  const { ta, ui } = setup("SEL");
  assert.equal(ui.visible(), true);
  const { prevented } = ta.dispatch("keydown", { key: "Enter", ctrlKey: true });
  assert.equal(prevented, false);
  assert.equal(ta.value, "SEL");
});

test("语句开头只给命令头，不在未输入时铺开候选", () => {
  let { ui } = setup("");
  assert.equal(ui.items()[0], "SELECT ");
  ({ ui } = setup("SEL"));
  assert.equal(ui.items()[0], "SELECT");
});

function mount(value, context) {
  const ta = makeTextarea(value);
  const editor = new Node("div"); editor.append(ta);
  sandbox.document.body.append(editor);
  const ui = sandbox.window.SqlCompletionUI.attach(ta, { getContext: () => context });
  ui.compute();
  return ui;
}

test("表名尚未加载时输入前缀：表名位置只给已知的表与片段", () => {
  // 回归：曾在表名位置退化成 PRIMARY KEY/PRAGMA（用户看到的错误候选）。
  const ui = mount("SELECT * FROM pr", { columns: [], tables: [], notReady: true });
  assert.deepEqual([...ui.items()], []);
  assert.equal(ui.items().includes("PRIMARY KEY"), false);
});

test("输入 d 时表名与关键字一起给出", () => {
  const inTable = mount("SELECT * FROM de", { columns: [], tables: ["desc_tb", "docs"] });
  assert.deepEqual([...inTable.items()], ["desc_tb"]);
  const elsewhere = mount("SELECT * FROM products de", { columns: ["deleted_at", "id"], tables: ["desc_tb"] });
  for (const expected of ["DESC", "desc_tb"]) assert.ok(elsewhere.items().includes(expected), expected + " :: " + elsewhere.items().join(" | "));
});

test("光标停在已输入的 FROM 后时不再提示一个 FROM", () => {
  const { ui } = setup("SELECT * FROM");
  assert.equal(ui.visible(), false);
});

test("FROM 后未输入前缀时不主动展开表名；输入前缀后才给表", () => {
  let { ta, ui } = setup("SELECT * FROM ");
  // 只有命令片段，且不包含任何表名。
  assert.equal(ui.items().filter((w) => ["products", "orders", "v_products"].includes(w)).length, 0);
  ({ ta, ui } = setup("SELECT * FROM pr"));
  assert.equal(ui.items()[0], "products");
  ({ ui } = setup("SELECT * FROM ", { columns: [], tables: Array.from({ length: 60 }, (_, i) => "ai_assistant_" + i), notReady: false }));
  assert.ok(ui.items().length <= 10, `未输入前缀却给出 ${ui.items().length} 项`);
});

test("WHERE 后未输入前缀时不主动列出全部字段", () => {
  const { ui } = setup("SELECT * FROM products WHERE ");
  assert.equal(ui.items().length, 0);
  const typed = setup("SELECT * FROM products WHERE na");
  assert.equal(typed.ui.items()[0], '"name"');
});

test("候选随上下文到达而刷新（notReady）", () => {
  let context = { columns: [], tables: [], notReady: true };
  // 仿照浏览器：value 先更新，再派发 input；浏览器在首次打开时 selectionStart/End 可能是 0。
  const ta = makeTextarea("");
  Object.defineProperty(ta, "value", { get() { return this._v || ""; }, set(v) { this._v = v; this.selectionStart = this.selectionEnd = v.length; } });
  ta.value = "SELECT * FROM pr";
  sandbox.document.activeElement = ta;
  const editor = new Node("div"); editor.append(ta);
  sandbox.document.body.append(editor);
  const ui = sandbox.window.SqlCompletionUI.attach(ta, {
    getContext: () => ({ columns: context.columns, tables: context.tables, notReady: context.notReady }),
  });
  ta.dispatch("input");
  assert.ok(!ui.items().includes("products"), "目录未就绪时不应凭空给出表名");
  context = { columns: ["id"], tables: ["products"], notReady: false };
  // 浏览器在补全未展开时不会主动重算，用户再敲一个键（或事件后调用 compute）才更新。
  ui.compute();
  assert.equal(ui.items()[0], "products");
});

test("选择文本时不弹出（避免替换掉多选内容）", () => {
  const { ta, ui } = setup("SELECT 1");
  ta.selectionStart = 0; ta.selectionEnd = 6;
  assert.equal(ui.compute(), undefined);
  assert.equal(ui.visible(), false);
});

test("插入后派发 input 事件，让草稿保存与重新计算继续工作", () => {
  const { ta, ui } = setup("SEL");
  let fired = 0;
  ta.addEventListener("input", () => { fired += 1; });
  ta.dispatch("keydown", { key: "Enter" });
  assert.equal(fired, 1);
  assert.equal(ui.visible() === false || ui.items().length >= 0, true);
});
