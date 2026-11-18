/*
 * 卡住自证（stall watch）
 * ============================================================================
 * 为什么需要它：传输层已经有 seq 补发、断线重连、resync，但「后端其实卡住了」
 * 和「模型正在长思考」在界面上长得一模一样——都是一个转不完的圈。用户能给的
 * 反馈只有一句「卡住了」，而我们没法据此区分网络半死、模型不吐字、还是看护
 * 没触发。
 *
 * 做法：记录最近一次收到任何 SSE 事件的时刻，流式期间超过阈值就把状态条文案
 * 换成「仍在等待模型响应（已 N 秒）」。这是纯展示改造，不改任何传输与状态机。
 *
 * 独立成模块的原因：app.js 是依赖 document 的裸脚本，无法在 Node 里直接加载；
 * 把判定逻辑抽出来才能用 node --test 覆盖边界（阈值、刚进入流式、事件恢复）。
 * 运行：node --test frontend/stall_watch.test.mjs
 */
(function (global) {
  "use strict";

  const STALL_HINT_SECONDS = 45;

  /**
   * 计算「静默了多少秒」。
   * @param {number} now        当前时间戳（ms）
   * @param {number} lastEventAt 最近一次事件时间戳（ms），0/null 表示还没有事件
   * @returns {number} 非负整数秒
   */
  function idleSeconds(now, lastEventAt) {
    if (!lastEventAt) return 0;
    const diff = now - lastEventAt;
    // 时钟回拨或事件时间在未来时不要算出负数，否则会显示「已 -3 秒」
    if (!(diff > 0)) return 0;
    return Math.floor(diff / 1000);
  }

  /**
   * 是否该显示等待提示。
   * @param {number} now
   * @param {{ streaming: boolean, lastEventAt: number }} state
   * @returns {boolean}
   */
  function shouldShowStallHint(now, state) {
    if (!state || !state.streaming) return false;
    return idleSeconds(now, state.lastEventAt) >= STALL_HINT_SECONDS;
  }

  /**
   * 生成状态条文案：未超阈值时返回 fallback（保留各阶段原有文案）。
   * @param {number} idle
   * @param {string} fallback
   */
  function stallLabel(idle, fallback) {
    if (idle < STALL_HINT_SECONDS) return fallback;
    return `仍在等待模型响应（已 ${idle} 秒）`;
  }

  const api = { STALL_HINT_SECONDS, idleSeconds, shouldShowStallHint, stallLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.StallWatch = api;
})(typeof window !== "undefined" ? window : globalThis);
