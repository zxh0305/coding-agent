/*
 * frontend/queue_state.js 的单元测试（Node 原生 test runner，无需依赖）
 * 运行：node --test frontend/queue_state.test.mjs
 *
 * 这个文件的主要价值是把「回合编排」从"只有手点页面才能发现问题"变成可测。
 * 首条用例 test_queued_messages_survive_interrupted_round 就是本项目真实
 * 发生过的那次 bug：断线重连走异常收尾，漏了推进队列 → 排队消息永久卡住，
 * 且新消息会插队到它们前面。
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  createTurnState, decideSend, nextDispatch, endRound,
  enqueue, dropQueued, promoteQueued, shiftQueued, dropSessionQueued,
} = require("./queue_state.js");

const msg = (text, sessionId = "s1") => ({ text, payloadAtts: [], sessionId });

/* ---------------------------------------------------------------------------
 * 1. 发送决策：队列非空就不能插队（原始 bug 的直接回归）
 * ------------------------------------------------------------------------- */

test("idle 且队列为空：直接发送", () => {
  const st = createTurnState();
  assert.equal(decideSend(st, "s1").action, "send");
});

test("正在生成：排队", () => {
  const st = { ...createTurnState(), streaming: true };
  assert.deepEqual(decideSend(st, "s1"), { action: "queue", reason: "streaming" });
});

test("未在生成但队列有积压：仍然排队——不许插队", () => {
  // 这正是断线重连后的状态：streaming 已被复位成 false，队列里还压着消息。
  // 只判 streaming 的实现会让新消息插队（原始 bug）。
  const st = { ...createTurnState(), streaming: false,
               pendingQueue: [msg("B"), msg("C")] };
  const d = decideSend(st, "s1");
  assert.equal(d.action, "queue");
  assert.equal(d.reason, "queue-not-empty");
});

test("别的会话的积压不影响本会话直接发送", () => {
  const st = { ...createTurnState(),
               pendingQueue: [msg("别的", "s2")] };
  assert.equal(decideSend(st, "s1").action, "send");
});

/* ---------------------------------------------------------------------------
 * 2. 回合结束：两条路径都必须推进队列
 * ------------------------------------------------------------------------- */

test("断线中断收尾必须推进队列（原始 bug 的回归）", () => {
  const actions = endRound(createTurnState(), "interrupted");
  assert.ok(actions.includes("advanceQueue"),
    "中断收尾漏了 advanceQueue → 排队消息永久卡死（真实 bug）");
});

test("正常收尾同样推进队列", () => {
  const actions = endRound(createTurnState(), "normal");
  assert.ok(actions.includes("advanceQueue"));
});

test("两条路径的公共半边完全一致", () => {
  const common = ["clearMetaTimer", "setStreaming:false", "clearNonce", "advanceQueue"];
  for (const kind of ["normal", "interrupted"]) {
    const actions = endRound(createTurnState(), kind);
    for (const a of common) {
      assert.ok(actions.includes(a), `${kind} 缺少公共动作 ${a}`);
    }
  }
});

test("中断的回合不许宣称成功：不标记已读", () => {
  const actions = endRound(createTurnState(), "interrupted");
  assert.ok(!actions.includes("markSeen"),
    "回合没跑完却标记已读/通知完成 = 对用户撒谎");
  assert.ok(actions.includes("appendInterruptNote"));
});

test("正常回合标记已读", () => {
  assert.ok(endRound(createTurnState(), "normal").includes("markSeen"));
});

/* ---------------------------------------------------------------------------
 * 3. 派发：FIFO，不跨会话跳队
 * ------------------------------------------------------------------------- */

test("空闲且队首属于当前会话：可派发队首", () => {
  const st = { ...createTurnState(), pendingQueue: [msg("B"), msg("C")] };
  const r = nextDispatch(st, "s1");
  assert.equal(r.ready, true);
  assert.equal(r.item.text, "B", "必须先发队首，顺序不能乱");
});

test("生成中不派发", () => {
  const st = { ...createTurnState(), streaming: true, pendingQueue: [msg("B")] };
  assert.equal(nextDispatch(st, "s1").ready, false);
});

test("队首属于别的会话：不跳过、不派发（切回来再发）", () => {
  const st = { ...createTurnState(), pendingQueue: [msg("B", "s2"), msg("C", "s1")] };
  const r = nextDispatch(st, "s1");
  assert.equal(r.ready, false);
  assert.equal(r.reason, "other-session");
  // 关键：不能因为队首是别的会话就把 C 提前发出去——那既破坏 FIFO，
  // 又会让 C 的消息渲染时机与用户预期不符
});

test("空队列不派发", () => {
  assert.equal(nextDispatch(createTurnState(), "s1").ready, false);
});

/* ---------------------------------------------------------------------------
 * 4. 队列增删改是纯操作
 * ------------------------------------------------------------------------- */

test("纯函数不改原状态", () => {
  const st = createTurnState();
  const st2 = enqueue(st, msg("B"));
  assert.equal(st.pendingQueue.length, 0, "原状态必须保持不变（便于回滚与测试）");
  assert.equal(st2.pendingQueue.length, 1);
});

test("出队返回队首并从队列移除", () => {
  let st = enqueue(enqueue(createTurnState(), msg("B")), msg("C"));
  const { state, item } = shiftQueued(st);
  assert.equal(item.text, "B");
  assert.deepEqual(state.pendingQueue.map((m) => m.text), ["C"]);
});

test("提到队首：立即执行的语义", () => {
  let st = enqueue(enqueue(createTurnState(), msg("B")), msg("C"));
  st = promoteQueued(st, st.pendingQueue[1]);
  assert.deepEqual(st.pendingQueue.map((m) => m.text), ["C", "B"]);
});

test("本来就是队首时 promote 不变", () => {
  const st = enqueue(createTurnState(), msg("B"));
  assert.equal(promoteQueued(st, st.pendingQueue[0]), st);
});

test("删除指定条目", () => {
  let st = enqueue(enqueue(createTurnState(), msg("B")), msg("C"));
  st = dropQueued(st, st.pendingQueue[0]);
  assert.deepEqual(st.pendingQueue.map((m) => m.text), ["C"]);
});

test("删除会话：只作废该会话的排队消息", () => {
  let st = enqueue(enqueue(createTurnState(), msg("B", "s1")), msg("C", "s2"));
  st = dropSessionQueued(st, "s1");
  assert.deepEqual(st.pendingQueue.map((m) => m.text), ["C"]);
});

/* ---------------------------------------------------------------------------
 * 5. 端到端场景复演：原始 bug 的完整时序
 * ------------------------------------------------------------------------- */

test("场景复演：排队 → 断线 → 重连收尾 → 新消息不插队", () => {
  // 1) 回合 A 在跑，用户排了 B、C
  let st = { ...createTurnState(), streaming: true };
  st = enqueue(st, msg("B"));
  st = enqueue(st, msg("C"));

  // 2) 流断了；重连时服务端说 running=false → 走中断收尾
  const actions = endRound(st, "interrupted");
  st = { ...st, streaming: false };
  assert.ok(actions.includes("advanceQueue"));

  // 3) 收尾后应能立刻派发 B（不再是死等 turn_end）
  const d = nextDispatch(st, "s1");
  assert.equal(d.ready, true);
  assert.equal(d.item.text, "B");

  // 4) 若用户在 B 派发前又发了 D：D 必须排在 C 后面，不能插队
  assert.equal(decideSend(st, "s1").action, "queue");
});
