/*
 * 回合状态机与排队队列：纯逻辑，不碰 DOM
 * =====================================================================
 *
 * 背景（为什么要有这个文件）
 * ---------------------------------------------------------------------
 * 前端的「回合状态」与「排队队列」此前全部散在 app.js 里，靠一堆全局变量
 * （streaming / pendingQueue / myNonce / liveBubble …）互相配合。结果是同一
 * 类 bug 反复出现：刷新后状态条消失、切回进行中回合出现双思考窗、断线重连
 * 后"生成中"永远不消失——根因都是同一个：**"回合结束"这件事有多个来源
 * （服务端 turn_end 事件、重连时 caught_up.running=false 的判定、发送失败
 * 回滚），每个来源各写一份收尾逻辑，谁漏了一步就是一次线上 bug**。
 *
 * 最典型的一次：断线重连走"异常收尾"路径时漏了"推进排队队列"，于是用户
 * 排队的消息永远卡住；而此时 streaming 已被复位成 false，用户再发一条新
 * 消息会【插队】到那些积压消息前面（app.js 的 send() 只判了 streaming）。
 * 这个 bug 无法靠 review 发现——必须把"回合状态"变成可测的数据。
 *
 * 设计：数据 → 决策（纯函数）
 * ---------------------------------------------------------------------
 * 本模块不碰 DOM、不碰网络、不碰 localStorage，只做两件事：
 *   1. createTurnState()          回合状态的唯一真相（一个普通对象）
 *   2. 一组纯函数，输入状态 + 事件，输出「新状态 + 要执行的动作列表」
 *
 * 调用方（app.js）只负责把动作列表翻译成副作用（调 setStreaming、发网络
 * 请求、画 DOM）。这样：
 *   * 同一类 bug 在单测里就能复现（见 queue_state.test.mjs），不必手点页面；
 *   * "回合结束要做什么"只有一个答案，不会再出现两份收尾逻辑漂移。
 *
 * 与 blocks.js / render_blocks.js 的分工：那两个管"画出什么"，本模块管
 * "现在处于什么状态、下一步该干什么"。三层都是纯模块，三层都有单测。
 */

(function (root, factory) {
  // 浏览器（挂全局）与 Node（module.exports）双用：单测在 Node 里跑，
  // 页面里直接 <script> 引入，无需构建步骤。与 blocks.js / render_blocks.js
  // 同一套 UMD 写法——裸写 module.exports 会在浏览器里抛
  // "module is not defined"，整个文件失效（踩过一次）。
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CodingAgentQueueState = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

/* ---------------------------------------------------------------------------
 * 1. 状态
 * ------------------------------------------------------------------------- */

/**
 * 创建一个回合状态。调用方持有它，所有变更都经过下面的纯函数。
 *
 * @param {() => string|null} [currentSession] 当前会话 id 的读取器（切换会话
 *   时状态里的 currentSession 会过期，用它现读最准）
 */
function createTurnState() {
  return {
    streaming: false,      // 是否有回合正在生成（驱动发送/停止按钮）
    nonce: null,           // 本 tab 发起回合的回令（识别"这回合是我发的"）
    pendingQueue: [],      // 未发出的排队消息 { text, payloadAtts, sessionId }
    placeholderOpen: false, // 过程面板的展开是否只是"即时占位"
  };
}

/* ---------------------------------------------------------------------------
 * 2. 排队：入队 / 派发决策
 * ------------------------------------------------------------------------- */

/**
 * 决定一条新消息该「直接发送」还是「排队」。
 *
 * 判据是【队列是否为空】，不是"此刻是否在流式输出"——这是本项目真实踩过的
 * 坑：断线后 streaming 被复位成 false，但队列里还有积压；只判 streaming
 * 会让新消息插队到积压消息前面执行，顺序被破坏，积压消息则永远卡住。
 *
 * @returns {{action: "send"|"queue", reason: string}}
 */
function decideSend(state, sessionId) {
  if (state.streaming) {
    return { action: "queue", reason: "streaming" };
  }
  const hasQueued = state.pendingQueue.some((it) => it.sessionId === sessionId);
  if (hasQueued) {
    return { action: "queue", reason: "queue-not-empty" };
  }
  return { action: "send", reason: "idle" };
}

/**
 * 决定下一条该派发哪条排队消息（不修改 state，纯查询）。
 *
 * 规则：没在生成、队首属于当前会话才发。队首属于别的会话时**不跳过**——
 * 跳过会破坏 FIFO，也会让消息渲染进错误的对话视图（用户的第二条消息
 * 冒到第一条前面）。调用方切回该会话时会再次调用本函数。
 *
 * @returns {{ready: boolean, item?: object, reason?: string}}
 */
function nextDispatch(state, currentSession) {
  if (state.streaming) return { ready: false, reason: "streaming" };
  if (!state.pendingQueue.length) return { ready: false, reason: "empty" };
  const item = state.pendingQueue[0];
  if (item.sessionId !== currentSession) {
    return { ready: false, reason: "other-session" };
  }
  return { ready: true, item };
}

/* ---------------------------------------------------------------------------
 * 3. 回合结束：两种来源，一个答案
 * ------------------------------------------------------------------------- */

/**
 * 回合结束的公共清理动作——**两条收尾路径都必须执行**。
 *
 * kind:
 *   "normal"      服务端 turn_end 事件（回合正常跑完，含被用户停止）
 *   "interrupted" 断线/重启后判定回合已死（caught_up.running === false）
 *
 * 返回要执行的动作名列表。两者共有的是：清计时器 / 复位流式态 / 清 nonce /
 * 推进队列。差别是"要不要宣称成功"：中断的回合没跑完，不能标记已读、不能
 * 弹"回答已完成"的桌面通知——那是在撒谎。但**队列推进两条路径都要**，
 * 这正是此前漏掉、导致排队消息卡死的那一步。
 */
function endRound(state, kind) {
  const actions = ["clearMetaTimer", "setStreaming:false", "clearNonce",
                   "advanceQueue"];
  if (kind === "interrupted") {
    actions.push("clearTraceRunning", "appendInterruptNote");
  } else {
    // 正常结束才有资格宣称结果可用
    actions.push("markSeen");
  }
  return actions;
}

/* ---------------------------------------------------------------------------
 * 4. 队列的纯增删改（返回新数组，不改原数组）
 * ------------------------------------------------------------------------- */

function enqueue(state, item) {
  return { ...state, pendingQueue: [...state.pendingQueue, item] };
}

function dropQueued(state, item) {
  return { ...state, pendingQueue: state.pendingQueue.filter((it) => it !== item) };
}

/** 把一条排队消息提到队首（「⬆ 立即」：停掉当前生成，马上发这一条）。 */
function promoteQueued(state, item) {
  const idx = state.pendingQueue.indexOf(item);
  if (idx <= 0) return state;
  const rest = state.pendingQueue.filter((it) => it !== item);
  return { ...state, pendingQueue: [item, ...rest] };
}

/** 出队队首（派发时调用）。 */
function shiftQueued(state) {
  const [first, ...rest] = state.pendingQueue;
  return { state: { ...state, pendingQueue: rest }, item: first };
}

/** 会话被删除：作废该会话的全部排队消息。 */
function dropSessionQueued(state, sessionId) {
  return { ...state,
           pendingQueue: state.pendingQueue.filter((it) => it.sessionId !== sessionId) };
}

  return {
    createTurnState, decideSend, nextDispatch, endRound,
    enqueue, dropQueued, promoteQueued, shiftQueued, dropSessionQueued,
  };
});
