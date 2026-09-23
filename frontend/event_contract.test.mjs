/*
 * 后端事件契约测试
 * =====================================================================
 * 运行：node --test frontend/event_contract.test.mjs
 *
 * 为什么需要它：二期重构时我曾按「常见命名」猜权限事件的字段（写成
 * request_id/name/arguments），而后端实际是 id/tool/input/reason——三个字段
 * 全错，权限卡拿不到任何信息。这类 bug 不会让页面崩，只是悄悄显示不全，
 * 极难发现。
 *
 * 本文件把后端事件协议【按源码里的真实字段】固化成用例：后端改了事件字段
 * 而前端没跟，这里立刻红。协议出处见：
 *   backend/agent.py  run() 的 docstring（事件序列表）
 *   backend/app.py    _run_round() 的 docstring（回合边界事件 + mid 规则）
 *
 * 事件协议（实证）：
 *   turn_start  {nonce, input, atts}                    注意：不带 user_mid
 *   round       {round, mid, wrap_up?}                  mid 由 app.py 现生成
 *   reasoning_delta {delta}                             【不带 mid】
 *   answer_delta    {delta, mid}
 *   tool_call   {name, arguments}                       arguments 是 JSON 字符串
 *   tool_result {name, result}
 *   permission_request {id, tool, input, reason}        input 可能是对象
 *   usage       {...}
 *   done        {answer, mid, elapsed_s, usage, stopped?, stopped_reason?}
 *   compacted   {summary, prompt_tokens, context}
 *   turn_end    {user_mid}                              mid 在此才回填
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { blocksFromEvents, createLiveTracker } = require("./blocks.js");

// 一条完整回合的事件流，字段严格照后端协议书写
function fullTurn() {
  return [
    { type: "turn_start", nonce: "abc", input: "改一下 a.py", atts: [] },
    { type: "round", round: 1, mid: "seg1" },
    { type: "reasoning_delta", delta: "先读文件" },
    { type: "answer_delta", mid: "seg1", delta: "我先看一下。" },
    { type: "tool_call", name: "read_file", arguments: '{"path":"a.py"}' },
    { type: "tool_result", name: "read_file", result: '{"ok":true,"result":"..."}' },
    { type: "round", round: 2, mid: "seg2" },
    { type: "answer_delta", mid: "seg2", delta: "改好了。" },
    {
      type: "done", mid: "seg2", answer: "改好了。", elapsed_s: 2.3,
      usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
    },
    { type: "turn_end", user_mid: "useruuid" },
  ];
}

test("契约：完整回合的事件流不产生未知/异常块", () => {
  const blocks = blocksFromEvents(fullTurn());
  const kinds = blocks.map((b) => b.kind);
  assert.deepEqual(kinds, ["user", "process", "answer", "meta"]);
  // process 内的子块类型只应是 note/reasoning/tool
  const proc = blocks.find((b) => b.kind === "process");
  for (const it of proc.items) {
    assert.ok(["note", "reasoning", "tool"].includes(it.kind), "意外的子块类型: " + it.kind);
  }
});

test("契约：turn_start 不带 user_mid（mid 在 turn_end 才回填）", () => {
  // 前端不能指望 turn_start 有 user_mid；缺了也不能崩
  const blocks = blocksFromEvents([{ type: "turn_start", nonce: "x", input: "你好", atts: [] }]);
  assert.equal(blocks[0].kind, "user");
  assert.equal(blocks[0].mid, null); // 后端此时确实没有 mid
  assert.equal(blocks[0].text, "你好");
});

test("契约：reasoning_delta 不带 mid，且不混入 answer", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "round", round: 1, mid: "seg1" },
    { type: "reasoning_delta", delta: "推理内容" }, // 后端刻意不带 mid
    { type: "answer_delta", mid: "seg1", delta: "回答内容" },
    { type: "done", mid: "seg1", answer: "回答内容", elapsed_s: 1 },
  ]);
  const proc = blocks.find((b) => b.kind === "process");
  const reasoning = proc.items.find((i) => i.kind === "reasoning");
  assert.equal(reasoning.text, "推理内容");
  const answer = blocks.find((b) => b.kind === "answer");
  assert.equal(answer.text, "回答内容");
  assert.ok(!answer.text.includes("推理内容"));
});

test("契约：tool_call 的 arguments 是 JSON 字符串，原样传给渲染层", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "tool_call", name: "apply_patch", arguments: '{"path":"a.py","search":"x","replace":"y"}' },
    { type: "tool_result", name: "apply_patch", result: '{"ok":true,"added":1,"removed":1}' },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  // 必须是字符串（渲染层 makeToolCallLine 自己 JSON.parse），不是对象
  assert.equal(typeof tool.arguments, "string");
  assert.equal(tool.status, "ok");
});

test("契约：permission_request 用 id/tool/input/reason（input 是对象）", () => {
  // 这是曾被我猜错的字段——本用例专门锁死
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "permission_request", id: "req-1", tool: "run_bash", input: { command: "rm -rf x" }, reason: "高危" },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(tool.name, "run_bash");          // 来自 tool，不是 name
  assert.equal(tool.permission.request_id, "req-1"); // 来自 id，不是 request_id
  assert.equal(tool.permission.reason, "高危");
  // input 是对象 → 序列化成字符串供渲染层用
  assert.equal(typeof tool.arguments, "string");
  assert.deepEqual(JSON.parse(tool.arguments), { command: "rm -rf x" });
});

test("契约：permission_request 的 input 也可能是字符串（不重复序列化）", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "permission_request", id: "r", tool: "run_bash", input: '{"command":"ls"}', reason: "x" },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(tool.arguments, '{"command":"ls"}');
});

test("契约：done 带 elapsed_s/usage，转成 meta 块", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "round", round: 1, mid: "s" },
    { type: "answer_delta", mid: "s", delta: "答" },
    { type: "done", mid: "s", answer: "答", elapsed_s: 3.7, usage: { total_tokens: 50 } },
  ]);
  const meta = blocks.find((b) => b.kind === "meta");
  assert.ok(meta);
  assert.equal(meta.elapsed, 3.7);
  assert.equal(meta.usage.total_tokens, 50);
});

test("契约：compacted 带 summary，转成 compact 块", () => {
  const blocks = blocksFromEvents([
    { type: "compacted", summary: "早期摘要", prompt_tokens: 500, context: {} },
  ]);
  assert.equal(blocks[0].kind, "compact");
  assert.equal(blocks[0].summary, "早期摘要");
});

test("契约：turn_end 不产块（只驱动落盘/队列）", () => {
  const before = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "round", round: 1, mid: "s" },
    { type: "done", mid: "s", answer: "答", elapsed_s: 1 },
  ]);
  const after = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "round", round: 1, mid: "s" },
    { type: "done", mid: "s", answer: "答", elapsed_s: 1 },
    { type: "turn_end", user_mid: "u" },
  ]);
  assert.deepEqual(after.map((b) => b.kind), before.map((b) => b.kind));
});

test("契约：usage 事件不产块（用量已随 done 归入 meta）", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", atts: [] },
    { type: "usage", prompt_tokens: 10, elapsed_s: 0.5 },
  ]);
  assert.ok(!blocks.some((b) => b.kind === "meta"), "usage 不该单独产 meta 块");
});

test("契约：权限流程的真实事件序（无 tool_call，只有 permission_request → tool_result）", () => {
  // 实证自 backend/agent.py 的 _run_round：命中 ask 时不发 tool_call，
  // 直接发 permission_request，用户决定后才发 tool_result。
  // 因此 permission_request 必须【自己建块】，tool_result 再把它回填成终态。
  const events = [
    { type: "turn_start", input: "x", atts: [] },
    { type: "permission_request", id: "req-1", tool: "run_bash", input: { command: "rm x" }, reason: "高危" },
    { type: "tool_result", name: "run_bash", result: '{"ok":false,"error":"权限拒绝：用户拒绝"}' },
  ];
  const tr = createLiveTracker();
  events.forEach((e) => tr.feed(e));
  const st = tr.state();

  // 只有一个工具块（不是 permission_request 一个 + tool_result 又一个）
  const tTools = st.items.filter((i) => i.kind === "tool");
  assert.equal(tTools.length, 1, "权限流程只应产出一个工具块");
  assert.equal(tTools[0].status, "denied");        // 已被结果回填成终态
  assert.equal(tTools[0].permission.request_id, "req-1"); // 权限信息仍在
  assert.equal(tTools[0].name, "run_bash");
  assert.equal(st.steps, 1);                        // 只算一步

  // 纯函数视角必须一致
  const pure = blocksFromEvents(events).find((b) => b.kind === "process");
  const pTools = pure.items.filter((i) => i.kind === "tool");
  assert.equal(pTools.length, 1);
  assert.equal(pTools[0].status, "denied");
  assert.equal(pTools[0].permission.request_id, "req-1");
  assert.deepEqual(
    tTools.map((t) => `${t.name}:${t.status}`),
    pTools.map((t) => `${t.name}:${t.status}`),
  );
});
