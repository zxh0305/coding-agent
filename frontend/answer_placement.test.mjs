/*
 * 答案落位算法的单元测试（Node 原生 test runner）
 * 运行：node --test frontend/answer_placement.test.mjs
 *
 * 背景：finalizeAnswer 定稿时要把答案组插进对话流，规则是"落在本回合之内"。
 * 回合进行中用户还能继续提问（steer），那些气泡直接 append 到 #chat 末尾，
 * 若答案只按 traceEl.after() 落位，就会插到后发的提问【上方】——页面呈现
 * "问题在下、回答在上"（2026-11-19 用户截图）。
 *
 * 这里用假 DOM 复刻 turnTailAnchor / nextTurnStart 的判定，锁住三条规则：
 *   1) 本回合提问之后没有别的回合内容 → 落在本回合过程卡之后（最后一个兄弟）
 *   2) 本回合提问之后有【另一回合】的提问 → 答案要落在那个提问【之前】
 *   3) 后面跟着别的回合的答案组/过程卡（带了归属键）→ 同样在它之前
 */

import { test } from "node:test";
import assert from "node:assert/strict";

// ---------- 极简假 DOM（只实现兄弟链与 dataset） ----------
class FakeEl {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.parentNode = null;
    this.children = [];   // 每个实例独立：曾挂在原型上导致所有节点共用一个数组
    this.dataset = {};
    this.className = "";
  }
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.children.indexOf(this);
    return i < 0 ? null : this.parentNode.children[i + 1] || null;
  }
  appendChild(ch) { ch.parentNode = this; this.children.push(ch); return ch; }
  after(node) { return this.parentNode.appendChild(node); }  // 追加到末尾
}

function mk(tag, attrs = {}) {
  const el = new FakeEl(tag);
  Object.assign(el.dataset, attrs);
  return el;
}
function chat(...nodes) {
  const c = mk("div");
  for (const n of nodes) c.appendChild(n);
  return c;
}

// ---------- 被测算法（逐字对应 app.js 的实现） ----------
function nextTurnStart(fromNode) {
  const ownMid = fromNode.dataset.mid;
  for (let n = fromNode.nextElementSibling; n; n = n.nextElementSibling) {
    const role = n.dataset?.role;
    if (role === "user" && n.dataset.mid && n.dataset.mid !== ownMid) return n;
    const forMid = n.dataset?.answerFor;
    if (forMid && forMid !== ownMid) return n;
    if (n.classList?.contains("trace") && n.dataset?.turnMid && n.dataset.turnMid !== ownMid) return n;
  }
  return null;
}
function turnTailAnchor(root, userMid) {
  const q = root.children.find(
    (n) => n.dataset.role === "user" && n.dataset.mid === userMid);
  if (!q) return null;
  const stopAt = nextTurnStart(q);
  let tail = q;
  for (let n = q.nextElementSibling; n && n !== stopAt; n = n.nextElementSibling) tail = n;
  return tail === q ? null : tail;
}
function placeAnswer(root, group, traceEl, userMid) {
  if (!userMid) { traceEl.after(group); return; }
  const anchor = turnTailAnchor(root, userMid);
  if (anchor) anchor.after(group); else traceEl.after(group);
}
// 假 DOM 的 after 是"追加到末尾"，这里补一个真正的"插入到某节点之后"
FakeEl.prototype.after = function (node) {
  const p = this.parentNode;
  node.parentNode = p;
  p.children.splice(p.children.indexOf(this) + 1, 0, node);
};

test("无追问：答案落在本回合过程卡与统计行之后（末尾）", () => {
  const q = mk("div", { role: "user", mid: "u1" });
  const trace = mk("details"); trace.className = "trace"; trace.dataset.turnMid = "u1";
  const meta = mk("div");
  const root = chat(q, trace, meta);
  const ans = mk("div", { answerFor: "u1" });
  placeAnswer(root, ans, trace, "u1");
  assert.deepEqual(root.children, [q, trace, meta, ans]);
});

test("回合中用户继续追问：答案必须落在追问【之前】", () => {
  const q = mk("div", { role: "user", mid: "u1" });
  const trace = mk("details"); trace.className = "trace"; trace.dataset.turnMid = "u1";
  const q2 = mk("div", { role: "user", mid: "u2" });   // 回合中插入的新提问
  const root = chat(q, trace, q2);
  const ans = mk("div", { answerFor: "u1" });
  placeAnswer(root, ans, trace, "u1");
  assert.deepEqual(root.children, [q, trace, ans, q2]);
});

test("后面是别的回合的答案组（带归属键）：同样落在它之前", () => {
  const q = mk("div", { role: "user", mid: "u1" });
  const trace = mk("details"); trace.className = "trace"; trace.dataset.turnMid = "u1";
  const ans2 = mk("div", { answerFor: "u0" });         // 更早回合的答案
  const root = chat(q, trace, ans2);
  const ans = mk("div", { answerFor: "u1" });
  placeAnswer(root, ans, trace, "u1");
  assert.deepEqual(root.children, [q, trace, ans, ans2]);
});

test("无归属信息（旧路径）：沿用 traceEl.after 原行为", () => {
  const trace = mk("details"); trace.className = "trace";
  const tail = mk("div");
  const root = chat(trace, tail);
  const ans = mk("div");
  placeAnswer(root, ans, trace, null);
  assert.deepEqual(root.children, [trace, ans, tail]);
});
