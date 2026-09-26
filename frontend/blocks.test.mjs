/*
 * frontend/blocks.js 的单元测试（Node 原生 test runner，无需依赖）
 * 运行：node --test frontend/blocks.test.mjs
 *
 * 重点验证三件事：
 *  1. 两条路径（实时事件 / 历史回放）对「同一回合」产出同构的块；
 *  2. 工具状态机（running → ok/err/denied/waiting）判定正确；
 *  3. 曾经出过 bug 的边界：过程说明不冒充思考、中间轮不占位、空回答不留空块。
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { blocksFromEvents, blocksFromHistory, createLiveTracker } = require("./blocks.js");

// 把块结构压成「形状签名」，便于跨路径比对（忽略具体文本差异）
function shape(blocks) {
  return blocks.map((b) => {
    if (b.kind === "process") {
      return "process(" + b.items.map((i) => (i.roundHead ? "round" : i.kind)).join(",") + ")";
    }
    return b.kind;
  });
}

test("实时：一次带工具调用的完整回合", () => {
  const events = [
    { type: "turn_start", input: "算一下 37*89+100", user_mid: "u1" },
    { type: "round", round: 1, mid: "a1" },
    { type: "answer_delta", mid: "a1", delta: "我算一下。" },
    { type: "tool_call", name: "calculator", arguments: '{"expression":"37*89+100"}' },
    { type: "tool_result", name: "calculator", result: '{"ok":true,"result":3393}' },
    { type: "round", round: 2, mid: "a2" },
    { type: "answer_delta", mid: "a2", delta: "等于 3393。" },
    { type: "done", mid: "a2", answer: "等于 3393。", elapsed_s: 1.2, usage: { prompt_tokens: 100 } },
  ];
  const blocks = blocksFromEvents(events);
  assert.deepEqual(shape(blocks), [
    "user",
    "process(round,tool,round)",
    "answer",
    "meta",
  ]);
  const proc = blocks.find((b) => b.kind === "process");
  assert.equal(proc.steps, 1);
  const tool = proc.items.find((i) => i.kind === "tool");
  assert.equal(tool.status, "ok");
  assert.equal(tool.name, "calculator");
});

test("回放：同一回合产出同构的块（两路径一致性）", () => {
  const msgs = [
    { role: "user", content: "算一下 37*89+100", mid: "u1" },
    {
      role: "assistant",
      content: null,
      tool_calls: [{ id: "c1", function: { name: "calculator", arguments: "{}" } }],
    }, // 中间轮：不占位
    { role: "tool", tool_call_id: "c1", content: '{"ok":true,"result":3393}' },
    {
      role: "assistant",
      content: "等于 3393。",
      mid: "a2",
      trace: [
        { type: "round", round: 1 },
        { type: "process_text", round: 1, text: "我算一下。" },
        { type: "tool_call", name: "calculator", arguments: '{"expression":"37*89+100"}' },
        { type: "tool_result", name: "calculator", result: '{"ok":true,"result":3393}' },
        { type: "round", round: 2 },
      ],
      stats: { elapsed_s: 1.2, usage: { prompt_tokens: 100 } },
    },
  ];
  const blocks = blocksFromHistory(msgs);
  assert.deepEqual(shape(blocks), [
    "user",
    "process(round,note,tool,round)",
    "answer",
    "meta",
  ]);
  // 与实时路径对比：回放多一个 note（过程说明落库了，实时里它进的是 answer 再降级）
  const proc = blocks.find((b) => b.kind === "process");
  const tool = proc.items.find((i) => i.kind === "tool");
  assert.equal(tool.status, "ok");
  assert.equal(proc.steps, 1);
});

test("工具状态：失败与权限拒绝分开成态", () => {
  const err = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "tool_call", name: "read_file", arguments: "{}" },
    { type: "tool_result", name: "read_file", result: '{"ok":false,"error":"文件不存在","hint":"先 list_dir"}' },
  ]);
  const errTool = err.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(errTool.status, "err");

  const denied = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "tool_call", name: "run_bash", arguments: "{}" },
    { type: "tool_result", name: "run_bash", result: '{"ok":false,"error":"权限拒绝：该命令被拦截"}' },
  ]);
  const dTool = denied.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(dTool.status, "denied");
});

test("工具状态：只有请求没有结果 = running（流式进行中）", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "tool_call", name: "grep", arguments: "{}" },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(tool.status, "running");
});

test("工具状态：权限确认卡 = waiting（按后端真实字段 id/tool/input/reason）", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "permission_request", id: "req1", tool: "run_bash", input: { command: "rm x" }, reason: "高危命令" },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(tool.status, "waiting");
  assert.equal(tool.name, "run_bash");
  assert.equal(tool.permission.request_id, "req1");
  assert.equal(tool.permission.reason, "高危命令");
  assert.equal(tool.arguments, '{"command":"rm x"}'); // input 对象被序列化成 arguments 字符串
});

test("思考流不混入回答（曾经的真 bug：过程说明冒充思考）", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "round", round: 1, mid: "a1" },
    { type: "reasoning_delta", delta: "先看目录" },
    { type: "reasoning_delta", delta: "再读文件" },
    { type: "answer_delta", mid: "a1", delta: "答案是 42" },
    { type: "done", mid: "a1", answer: "答案是 42", elapsed_s: 0.5 },
  ]);
  const proc = blocks.find((b) => b.kind === "process");
  const reasoning = proc.items.find((i) => i.kind === "reasoning");
  assert.equal(reasoning.text, "先看目录再读文件"); // 连续 delta 合并
  const answer = blocks.find((b) => b.kind === "answer");
  assert.equal(answer.text, "答案是 42"); // 推理没有混进回答
  assert.ok(!answer.text.includes("先看目录"));
});

test("空回答不留空块（曾经的真 bug：一堆空白方块）", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "round", round: 1, mid: "a1" },
    { type: "done", mid: "a1", answer: "", elapsed_s: 0.3 },
  ]);
  assert.ok(!blocks.some((b) => b.kind === "answer"), "空回答不该产出 answer 块");
});

test("回放：正文为空的 assistant 消息不画气泡", () => {
  const blocks = blocksFromHistory([
    { role: "user", content: "x", mid: "u1" },
    { role: "assistant", content: "", mid: "a1", trace: [{ type: "round", round: 1 }] },
  ]);
  assert.ok(!blocks.some((b) => b.kind === "answer"));
  assert.ok(blocks.some((b) => b.kind === "process")); // 过程仍在
});

test("回放：错误消息（error 标记）渲染成错误块，retryable 透传", () => {
  const blocks = blocksFromHistory([
    { role: "user", content: "你好", mid: "u1" },
    { role: "assistant", content: "❌ 连接中断", error: true, retryable: true, mid: "e1" },
  ]);
  // user 消息也会产出自己的块，错误块是最后一块
  assert.equal(blocks.length, 2);
  const errBlock = blocks[blocks.length - 1];
  assert.equal(errBlock.kind, "error");
  assert.equal(errBlock.text, "❌ 连接中断");
  assert.equal(errBlock.retryable, true);

  // 不带 error 标记的普通 assistant 消息不能被误判成错误
  const normal = blocksFromHistory([{ role: "assistant", content: "正常回答", mid: "a1" }]);
  assert.ok(normal.every((b) => b.kind !== "error"));
});

test("压缩卡：两条路径都产出 compact 块", () => {
  const live = blocksFromEvents([{ type: "compacted", summary: "早期对话摘要" }]);
  assert.equal(live[0].kind, "compact");
  assert.equal(live[0].summary, "早期对话摘要");

  const replay = blocksFromHistory([{ role: "compact", content: "早期对话摘要" }]);
  assert.equal(replay[0].kind, "compact");
  assert.equal(replay[0].summary, "早期对话摘要");
});

test("回放：用户附件（图片）被还原", () => {
  const blocks = blocksFromHistory([
    {
      role: "user",
      content: [
        { type: "text", text: "看这张图" },
        { type: "image_url", image_url: { url: "data:image/png;base64,AAA" } },
      ],
      mid: "u1",
    },
  ]);
  assert.equal(blocks[0].kind, "user");
  assert.equal(blocks[0].text, "看这张图");
  assert.equal(blocks[0].atts.length, 1);
  assert.equal(blocks[0].atts[0].kind, "image");
});

test("实时：流式未完成的回答带 streaming 标记", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "answer_delta", mid: "a1", delta: "正在输" },
  ]);
  const answer = blocks.find((b) => b.kind === "answer");
  assert.equal(answer.streaming, true);
});

test("边界：异常序列下孤儿 tool_result 不丢信息", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "tool_result", name: "grep", result: '{"ok":true}' },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.ok(tool.orphan);
  assert.equal(tool.status, "ok");
});

test("边界：空输入不崩", () => {
  assert.deepEqual(blocksFromEvents([]), []);
  assert.deepEqual(blocksFromEvents(null), []);
  assert.deepEqual(blocksFromHistory([]), []);
  assert.deepEqual(blocksFromHistory(undefined), []);
});

// ---------- 实时追踪器（增量渲染共用配对逻辑）----------

test("追踪器：tool_call → tool_open，tool_result → tool_close 并配对", () => {
  const tr = createLiveTracker();
  tr.feed({ type: "turn_start", input: "x" });
  const open = tr.feed({ type: "tool_call", name: "read_file", arguments: "{}" });
  assert.equal(open.kind, "tool_open");
  assert.equal(open.tool.status, "running");

  const close = tr.feed({ type: "tool_result", name: "read_file", result: '{"ok":true}' });
  assert.equal(close.kind, "tool_close");
  assert.equal(close.tool, open.tool); // 同一个块对象
  assert.equal(close.tool.status, "ok");
});

test("追踪器：与 blocksFromEvents 配对结果一致（同一套逻辑）", () => {
  const events = [
    { type: "turn_start", input: "x" },
    { type: "round", round: 1 },
    { type: "tool_call", name: "grep", arguments: '{"pattern":"a"}' },
    { type: "tool_result", name: "grep", result: '{"ok":false,"error":"权限拒绝：xx"}' },
  ];
  const tr = createLiveTracker();
  for (const e of events) tr.feed(e);
  const viaTracker = tr.state();

  const viaPure = blocksFromEvents(events).find((b) => b.kind === "process");
  // 步数、工具名、状态三者应完全一致
  assert.equal(viaTracker.steps, viaPure.steps);
  const t1 = viaTracker.items.find((i) => i.kind === "tool");
  const t2 = viaPure.items.find((i) => i.kind === "tool");
  assert.equal(t1.name, t2.name);
  assert.equal(t1.status, t2.status);
  assert.equal(t1.status, "denied");
});

test("追踪器：权限请求产 tool_wait（按后端真实字段）", () => {
  const tr = createLiveTracker();
  tr.feed({ type: "turn_start", input: "x" });
  const w = tr.feed({ type: "permission_request", id: "r1", tool: "run_bash", input: { command: "rm x" }, reason: "高危" });
  assert.equal(w.kind, "tool_wait");
  assert.equal(w.tool.status, "waiting");
  assert.equal(w.tool.name, "run_bash");
  assert.equal(w.tool.permission.request_id, "r1");
});

test("追踪器：turn_start 重置上一回合的记账", () => {
  const tr = createLiveTracker();
  tr.feed({ type: "turn_start", input: "第一回合" });
  tr.feed({ type: "tool_call", name: "read_file", arguments: "{}" });
  assert.equal(tr.state().steps, 1);
  tr.feed({ type: "turn_start", input: "第二回合" });
  assert.equal(tr.state(), null); // 已重置
});

test("追踪器：无关事件返回 null，不误伤", () => {
  const tr = createLiveTracker();
  assert.equal(tr.feed({ type: "answer_delta", delta: "hi" }), null);
  assert.equal(tr.feed({ type: "reasoning_delta", delta: "think" }), null);
  assert.equal(tr.feed({ type: "done", answer: "x" }), null);
  assert.equal(tr.feed(null), null);
});
