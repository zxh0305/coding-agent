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
const { blocksFromEvents, blocksFromHistory } = require("./blocks.js");

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

test("工具状态：权限确认卡 = waiting，并带请求信息", () => {
  const blocks = blocksFromEvents([
    { type: "turn_start", input: "x", user_mid: "u1" },
    { type: "permission_request", name: "run_bash", arguments: '{"command":"rm x"}', request_id: "req1", reason: "高危命令" },
  ]);
  const tool = blocks.find((b) => b.kind === "process").items.find((i) => i.kind === "tool");
  assert.equal(tool.status, "waiting");
  assert.equal(tool.permission.request_id, "req1");
  assert.equal(tool.permission.reason, "高危命令");
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
