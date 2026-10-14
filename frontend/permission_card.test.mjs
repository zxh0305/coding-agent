/*
 * 确认卡正文渲染的单元测试（Node 原生 test runner，零依赖）
 * 运行：node --test frontend/permission_card.test.mjs
 *
 * 回归背景：默认「确认」模式下每次写文件都会弹确认卡，而弹卡恰恰是用户唯一
 * 需要判断"它要干什么"的时刻。旧实现一律 `JSON.stringify(input, null, 2)`——
 * 对 apply_patch 就是 {path, search, replace} 的原始 JSON，带字面 \n，完全读不出
 * 改了什么。而 buildDiffBody 早就写好了红绿 diff，只是接在了工具【结果】卡上
 * （即改完之后），接错了时刻。
 *
 * 这个文件用极简假 DOM 验证：写文件类走 diff、其它工具回退 JSON。
 * app.js 不是模块（整文件顶层执行），所以这里把 buildPermissionBody 与它依赖的
 * buildDiffBody 按源码语义复制一份来测——与原实现的一致性由 smoke 手动核对；
 * 真正的价值是锁住"写文件必须渲染成 diff"这条行为契约。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

// ---------- 极简假 DOM（只实现被测代码用到的 API） ----------

class FakeEl {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = "";
    this.textContent = "";
  }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { this.children.push(...cs); }
  get childElementCount() { return this.children.length; }
  /** 深度优先收集节点，便于断言 */
  all() {
    const out = [];
    const walk = (n) => { out.push(n); (n.children || []).forEach(walk); };
    walk(this);
    return out;
  }
  findByClass(cls) { return this.all().filter((n) => String(n.className).split(" ").includes(cls)); }
}

const el = (tag) => new FakeEl(tag);

// ---------- 被测逻辑（与 frontend/app.js 中同名函数语义一致） ----------

function buildDiffBody(search, replace) {
  const wrap = el("div");
  wrap.className = "diff";
  const push = (text, cls, sign) => {
    if (!text) return;
    const lines = text.replace(/\n$/, "").split("\n");
    for (const ln of lines) {
      const row = el("div");
      row.className = "diff-line " + cls;
      const mark = el("span");
      mark.className = "diff-sign";
      mark.textContent = sign;
      const body = el("span");
      body.className = "diff-text";
      body.textContent = ln || " ";
      row.append(mark, body);
      wrap.appendChild(row);
    }
  };
  push(search, "del", "−");
  push(replace, "add", "+");
  if (!wrap.childElementCount) {
    const empty = el("div");
    empty.className = "diff-line";
    empty.textContent = "（无内容）";
    wrap.appendChild(empty);
  }
  return wrap;
}

function buildPermissionBody(evt) {
  const input = evt.input;
  const args = (input && typeof input === "object") ? input : null;
  const wrap = el("div");
  if (args && evt.tool === "apply_patch"
      && typeof args.search === "string" && typeof args.replace === "string") {
    wrap.className = "perm-diff";
    if (args.path) {
      const p = el("div");
      p.className = "perm-path";
      p.textContent = args.path;
      wrap.appendChild(p);
    }
    wrap.appendChild(buildDiffBody(args.search, args.replace));
    return wrap;
  }
  if (args && evt.tool === "write_file" && typeof args.content === "string") {
    wrap.className = "perm-diff";
    if (args.path) {
      const p = el("div");
      p.className = "perm-path";
      p.textContent = args.path;
      wrap.appendChild(p);
    }
    wrap.appendChild(buildDiffBody("", args.content));
    return wrap;
  }
  const pre = el("pre");
  pre.textContent = typeof input === "string" ? input : JSON.stringify(input, null, 2);
  wrap.appendChild(pre);
  return wrap;
}

// ---------- 用例 ----------

test("apply_patch 确认卡渲染成红绿 diff，不是转义 JSON", () => {
  const body = buildPermissionBody({
    tool: "apply_patch",
    input: { path: "src/a.py", search: "x = 1", replace: "x = 2" },
  });
  assert.equal(body.className, "perm-diff");
  const del = body.findByClass("del");
  const add = body.findByClass("add");
  assert.equal(del.length, 1, "应有一行删除");
  assert.equal(add.length, 1, "应有一行新增");
  assert.equal(del[0].findByClass("diff-text")[0].textContent, "x = 1");
  assert.equal(add[0].findByClass("diff-text")[0].textContent, "x = 2");
  // 关键：路径作为可读标题给出
  assert.equal(body.findByClass("perm-path")[0].textContent, "src/a.py");
  // 不应该出现原始 JSON 的转义串
  const joined = body.all().map((n) => n.textContent).join(" ");
  assert.ok(!joined.includes("\\n"), "不得出现转义换行");
});

test("write_file 确认卡把新内容渲染成绿色新增", () => {
  const body = buildPermissionBody({
    tool: "write_file",
    input: { path: "new.txt", content: "line1\nline2" },
  });
  assert.equal(body.className, "perm-diff");
  assert.equal(body.findByClass("add").length, 2);
  assert.equal(body.findByClass("del").length, 0, "新建文件没有删除行");
});

test("非写文件工具回退到格式化 JSON", () => {
  const body = buildPermissionBody({
    tool: "run_bash",
    input: { command: "npm install", timeout: 300 },
  });
  assert.equal(body.className, "");
  const pre = body.all().find((n) => n.tagName === "PRE");
  assert.ok(pre, "应有一个 pre 容器");
  assert.ok(pre.textContent.includes("npm install"));
  assert.ok(pre.textContent.includes("300"), "参数应完整可见");
});

test("畸形输入不崩：字符串 / null / 缺字段", () => {
  for (const input of ["raw string", null, undefined,
                       { path: "a" },                     // 缺 search/replace
                       { search: "a", replace: "b" }]) {  // 缺 path
    const body = buildPermissionBody({ tool: "apply_patch", input });
    assert.ok(body, `input=${JSON.stringify(input)} 不应崩`);
    assert.ok(body.all().length > 0);
  }
});

test("空 search/replace 仍产生占位行而非空白卡", () => {
  const body = buildPermissionBody({
    tool: "apply_patch",
    input: { path: "a.txt", search: "", replace: "" },
  });
  assert.ok(body.findByClass("diff-line").length >= 1, "空 diff 要有占位行");
});
