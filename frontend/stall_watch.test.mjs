/*
 * frontend/stall_watch.js 的单元测试（Node 原生 test runner，无需依赖）
 * 运行：node --test frontend/stall_watch.test.mjs
 *
 * 覆盖的是「卡住自证」最容易出错的四个边界：
 *  1. 阈值以下不打扰（不能一进流式就报等待）；
 *  2. 阈值以上如实显示秒数；
 *  3. 时钟回拨/事件时间在未来不能算出负数；
 *  4. 非流式状态永不提示（空闲时不该冒出来）。
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { STALL_HINT_SECONDS, idleSeconds, shouldShowStallHint, stallLabel } = require("./stall_watch.js");

const T0 = 1_700_000_000_000; // 固定基准时间，避免依赖真实时钟

test("阈值以下是 0~44 秒，且不显示提示", () => {
  for (const sec of [0, 1, 30, 44]) {
    const now = T0 + sec * 1000;
    assert.equal(idleSeconds(now, T0), sec);
    assert.equal(shouldShowStallHint(now, { streaming: true, lastEventAt: T0 }), false, `${sec}s 不该提示`);
  }
});

test("达到阈值（45 秒）即显示，并带正确秒数", () => {
  const now = T0 + STALL_HINT_SECONDS * 1000;
  assert.equal(idleSeconds(now, T0), 45);
  assert.equal(shouldShowStallHint(now, { streaming: true, lastEventAt: T0 }), true);
  assert.equal(stallLabel(45, "正在处理"), "仍在等待模型响应（已 45 秒）");
});

test("秒数持续增长（事件恢复后重新计数由调用方重置 lastEventAt）", () => {
  assert.equal(idleSeconds(T0 + 90_500, T0), 90);   // 91.5s 向下取整到 90
  const resumed = T0 + 120_000;                      // 模拟收到新事件
  assert.equal(idleSeconds(resumed + 1_000, resumed), 1, "新事件后应从 1 秒重新计");
});

test("没有事件时间戳（0/null）时不提示、不显示负数", () => {
  assert.equal(idleSeconds(T0, 0), 0);
  assert.equal(idleSeconds(T0, null), 0);
  assert.equal(shouldShowStallHint(T0, { streaming: true, lastEventAt: 0 }), false);
});

test("时钟回拨或事件时间在未来，返回 0 而不是负数", () => {
  assert.equal(idleSeconds(T0, T0 + 5_000), 0);
  assert.equal(stallLabel(0, "正在处理"), "正在处理");
});

test("非流式状态永不提示（空闲时不该冒出等待文案）", () => {
  const now = T0 + 600 * 1000;
  assert.equal(shouldShowStallHint(now, { streaming: false, lastEventAt: T0 }), false);
  assert.equal(shouldShowStallHint(now, null), false);
});

test("阈值以下返回原有阶段文案，不被覆盖", () => {
  assert.equal(stallLabel(10, "正在运行检查"), "正在运行检查");
  assert.equal(stallLabel(STALL_HINT_SECONDS - 1, "正在整理验证结果"), "正在整理验证结果");
});
