/*
 * 消息语义块：把「实时事件流」与「历史回放」两种形状的数据，
 * 归一化成同一套 Block 结构。
 * =====================================================================
 *
 * 背景：改造前，实时流（SSE 事件）与历史回放（/messages 的存储行）各写
 * 一套渲染代码，同一语义在两处实现——两边一不同步就出 bug（回放里过程
 * 说明冒充思考、回放漏带 tool_calls、点定位条冒出空白方块）。
 *
 * 本模块只做「数据 → 结构」的纯转换，不碰 DOM：
 *   blocksFromEvents(events)  实时：SSE 事件序列 → Block[]
 *   blocksFromHistory(msgs)   回放：存储消息序列 → Block[]
 * 两者输出同构，交给同一个渲染器 renderBlocks() 画（渲染在 app.js）。
 *
 * 纯函数的好处：可单测（test_blocks.mjs），不依赖浏览器，也就没有
 * 「只有手点页面才能发现不一致」的盲区。
 *
 * ── Block 结构 ────────────────────────────────────────────────────────
 *   { kind, ... }
 *   kind = "user"      用户提问   { text, atts, mid }
 *        | "process"   过程容器   { steps, elapsed, items: Block[] }
 *        | "tool"      工具调用   { name, arguments, result, status }
 *        | "reasoning" 思考流     { text }
 *        | "note"      过程说明   { text }
 *        | "answer"    最终回答   { text, mid, streaming }
 *        | "compact"   压缩卡     { summary }
 *        | "error"     错误/中断  { text }
 *        | "meta"      统计行     { elapsed, usage }
 *
 * process.items 里只会出现 tool / reasoning / note 三种子块。
 *
 * ── 工具状态（6 态合并为 5 态）────────────────────────────────────────
 *   "running"  已请求、尚未有结果（原 pending/running 合并：本项目工具
 *              串行执行，同时最多一个在跑，区分二者没有实际意义）
 *   "ok"       结果 ok:true
 *   "err"      结果 ok:false
 *   "denied"   结果为权限拒绝（error 以「权限拒绝」开头）
 *   "waiting"  权限确认卡弹出、等用户决定
 * 终态：ok / err / denied。
 */

(function (root, factory) {
  // 浏览器（挂全局）与 Node（module.exports）双用：单测在 Node 里跑，
  // 页面里直接 <script> 引入，无需构建步骤。
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CodingAgentBlocks = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------- 小工具 ----------

  // 工具结果统一是 JSON 字符串信封：{"ok":bool,"error":str,"hint":str,...}
  // 解析失败不算错（有些工具直接回纯文本），返回 null 让调用方按原样处理。
  function parseResult(result) {
    if (typeof result !== "string") return null;
    try {
      const v = JSON.parse(result);
      return v && typeof v === "object" ? v : null;
    } catch (_e) {
      return null;
    }
  }

  // 由一次工具调用的「请求 + 结果」推出状态。
  // 结果缺席 = 还在跑；结果存在但解析不出信封 = 按成功算（工具回纯文本）。
  function toolStatus(result) {
    if (result == null) return "running";
    const env = parseResult(result);
    if (!env) return "ok";
    if (env.ok === false) {
      const err = typeof env.error === "string" ? env.error : "";
      // 权限拒绝是可操作的特殊失败（用户可改权限模式），单独成一态
      return err.indexOf("权限拒绝") === 0 ? "denied" : "err";
    }
    return "ok";
  }

  function isObj(x) {
    return x && typeof x === "object" && !Array.isArray(x);
  }

  // ---------- 实时：SSE 事件 → Block[] ----------

  /*
   * 实时路径的关键点：工具调用与结果在事件流里是**两条独立事件**
   * （tool_call / tool_result），需要按到达顺序配对成一个 tool 块。
   * 配对规则：tool_call 先建成 running 块压进当前 process；tool_result
   * 到达时回填最近一个「同名且仍 running」的块。
   *
   * 事件类型（实证自 backend/agent.py）：
   *   turn_start / round / reasoning_delta / answer_delta / tool_call /
   *   tool_result / permission_request / usage / done / compacted /
   *   turn_end / error / stopped / system_reminder
   */
  function blocksFromEvents(events) {
    const blocks = [];
    let process = null;      // 当前 process 块（一个回合一个）
    let answer = null;       // 当前 answer 块（done 之前流式累积）
    let lastTool = null;     // 最近一个 running 的 tool 块，供 result 回填

    function ensureProcess() {
      if (!process) {
        process = { kind: "process", steps: 0, elapsed: null, items: [] };
        blocks.push(process);
      }
      return process;
    }

    function pushItem(item) {
      ensureProcess().items.push(item);
      return item;
    }

    for (const evt of Array.isArray(events) ? events : []) {
      if (!isObj(evt)) continue;
      const t = evt.type;

      if (t === "turn_start") {
        // 新回合：重置块级游标（process 每回合一个，随回合开始新建）
        process = null;
        answer = null;
        lastTool = null;
        blocks.push({
          kind: "user",
          text: evt.input || "",
          atts: Array.isArray(evt.atts) ? evt.atts : [],
          mid: evt.user_mid || null,
        });
      } else if (t === "round") {
        // 轮次标题不单独成块：作 process 内的分组锚点，渲染器据此画分隔标题
        pushItem({ kind: "note", text: `🧠 思考 · 第 ${evt.round} 轮`, roundHead: true, wrapUp: !!evt.wrap_up });
      } else if (t === "reasoning_delta") {
        // 思考流与回答是两个流：reasoning 归 process，绝不进 answer
        const items = ensureProcess().items;
        const last = items[items.length - 1];
        if (last && last.kind === "reasoning") last.text += evt.delta || "";
        else pushItem({ kind: "reasoning", text: evt.delta || "" });
      } else if (t === "answer_delta") {
        if (!answer) {
          answer = { kind: "answer", text: "", mid: evt.mid || null, streaming: true };
          blocks.push(answer);
        }
        answer.text += evt.delta || "";
        if (evt.mid) answer.mid = evt.mid;
      } else if (t === "tool_call") {
        // 过程说明（中间轮正文）不单独成块：它已随 answer_delta 进了 answer，
        // 但轮次一开就证明它不是最终答案——由渲染层在 round/tool_call 时降级。
        // 这里保持数据纯粹：tool_call 只产 tool 块。
        const tool = {
          kind: "tool",
          name: evt.name || "",
          arguments: evt.arguments != null ? evt.arguments : "{}",
          result: null,
          status: "running",
        };
        pushItem(tool);
        ensureProcess().steps += 1;
        lastTool = tool;
      } else if (t === "tool_result") {
        // 回填最近一个同名 running 块；找不到（异常序列）就补一个孤儿块，
        // 宁可多画一行也不静默丢信息。
        let target = null;
        const items = process ? process.items : [];
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (it.kind === "tool" && it.status === "running" && (!evt.name || it.name === evt.name)) {
            target = it;
            break;
          }
        }
        if (!target && lastTool && lastTool.status === "running") target = lastTool;
        if (target) {
          target.result = evt.result != null ? evt.result : "";
          target.status = toolStatus(target.result);
        } else {
          pushItem({
            kind: "tool",
            name: evt.name || "",
            arguments: "{}",
            result: evt.result != null ? evt.result : "",
            status: toolStatus(evt.result),
            orphan: true,
          });
        }
        lastTool = null;
      } else if (t === "permission_request") {
        // 确认卡挂在 process 里、对应工具位置：用 waiting 状态的 tool 块表示。
        // 后端字段是 {id, tool, input, reason}（见 agent.py 的事件表），
        // 不是 {request_id, name, arguments}——这里必须按真实字段读。
        pushItem({
          kind: "tool",
          name: evt.tool || "",
          arguments: evt.input != null
            ? (typeof evt.input === "string" ? evt.input : JSON.stringify(evt.input))
            : "{}",
          result: null,
          status: "waiting",
          permission: {
            request_id: evt.id || null,
            reason: evt.reason || "",
          },
        });
      } else if (t === "done") {
        // 定稿：answer 以 done.answer 为权威正文（非空时覆盖流式累积）
        const authoritative = typeof evt.answer === "string" ? evt.answer : "";
        if (!answer) {
          if (authoritative) {
            answer = { kind: "answer", text: authoritative, mid: evt.mid || null, streaming: false };
            blocks.push(answer);
          }
        } else {
          if (authoritative) answer.text = authoritative;
          answer.streaming = false;
          if (evt.mid) answer.mid = evt.mid;
          if (!answer.text) {
            // 空回答不留空块
            const i = blocks.indexOf(answer);
            if (i >= 0) blocks.splice(i, 1);
            answer = null;
          }
        }
        if (process) process.elapsed = evt.elapsed_s != null ? evt.elapsed_s : process.elapsed;
        if (evt.usage || evt.elapsed_s != null) {
          blocks.push({ kind: "meta", elapsed: evt.elapsed_s != null ? evt.elapsed_s : null, usage: evt.usage || null });
        }
        answer = null;
        lastTool = null;
      } else if (t === "compacted") {
        blocks.push({ kind: "compact", summary: evt.summary || "" });
        process = null;
      } else if (t === "error" || t === "stopped") {
        blocks.push({ kind: "error", text: evt.message || evt.text || (t === "stopped" ? "已停止生成" : "出错了") });
        process = null;
      }
      // usage / turn_end / system_reminder 等：usage 已随 done 归入 meta；
      // turn_end 只做落盘收尾，不产块；system_reminder 已在 trace 里作 note。
    }

    // 收尾：正在流式的 answer 保留 streaming 标记（渲染层画光标）
    return blocks;
  }

  // ---------- 回放：存储消息 → Block[] ----------

  /*
   * 存储形状（实证自 db / agent）：
   *   { role:"user",      content: str | [{type:"text"|"image_url"},...], mid }
   *   { role:"assistant", content: str, trace: [...], stats: {...}, mid }
   *   { role:"assistant", content: null, tool_calls: [...] }   ← 中间轮，不占位
   *   { role:"tool",      tool_call_id, content }
   *   { role:"compact",   content: 摘要 }
   *
   * trace 条目（与实时事件不同形，故必须单独映射）：
   *   {type:"round", round, wrap_up?} / {type:"reasoning", text} /
   *   {type:"process_text", text} / {type:"tool_call", name, arguments} /
   *   {type:"tool_result", name, result} / {type:"system_reminder", kind}
   */
  function blocksFromHistory(msgs) {
    const blocks = [];

    function traceToProcess(trace, elapsed) {
      const items = [];
      let steps = 0;
      let lastTool = null;
      for (const e of Array.isArray(trace) ? trace : []) {
        if (!isObj(e)) continue;
        if (e.type === "round") {
          items.push({ kind: "note", text: `🧠 思考 · 第 ${e.round} 轮`, roundHead: true, wrapUp: !!e.wrap_up });
        } else if (e.type === "reasoning") {
          if (e.text) items.push({ kind: "reasoning", text: e.text });
        } else if (e.type === "process_text") {
          items.push({ kind: "note", text: e.text || "", demoted: true });
        } else if (e.type === "tool_call") {
          const tool = {
            kind: "tool",
            name: e.name || "",
            arguments: e.arguments != null ? e.arguments : "{}",
            result: null,
            status: "running",
          };
          items.push(tool);
          lastTool = tool;
          steps += 1;
        } else if (e.type === "tool_result") {
          if (lastTool) {
            lastTool.result = e.result != null ? e.result : "";
            lastTool.status = toolStatus(lastTool.result);
            lastTool = null;
          } else {
            items.push({
              kind: "tool",
              name: e.name || "",
              arguments: "{}",
              result: e.result != null ? e.result : "",
              status: toolStatus(e.result),
              orphan: true,
            });
          }
        } else if (e.type === "system_reminder") {
          items.push({ kind: "note", text: "🔔 系统提醒", reminder: true });
        }
      }
      if (!items.length) return null;
      return { kind: "process", steps: steps, elapsed: elapsed != null ? elapsed : null, items: items };
    }

    for (const m of Array.isArray(msgs) ? msgs : []) {
      if (!isObj(m)) continue;

      if (m.role === "compact") {
        blocks.push({ kind: "compact", summary: m.content || "" });
        continue;
      }

      if (m.role === "user") {
        let text = "";
        let atts = [];
        if (Array.isArray(m.content)) {
          text = m.content
            .filter((p) => isObj(p) && p.type === "text")
            .map((p) => (typeof p.text === "string" && p.text.length > 600 ? p.text.slice(0, 600) + "…[附件内容已折叠]" : p.text || ""))
            .join("\n");
          atts = m.content
            .filter((p) => isObj(p) && p.type === "image_url")
            .map((p) => ({ kind: "image", name: "", preview: ((p.image_url || {}).url) || "" }));
        } else {
          text = typeof m.content === "string" ? m.content : "";
        }
        blocks.push({ kind: "user", text: text, atts: atts, mid: m.mid || null });
        continue;
      }

      if (m.role === "tool") {
        // 工具结果行：其 tool_call 在相邻 assistant 消息里，历史视图不单列，
        // 避免与 trace 里的 tool_result 重复（trace 才是过程轨迹的权威）。
        continue;
      }

      if (m.role === "assistant") {
        // 中间轮（带 tool_calls 且正文是过程说明）：不占位——那段文字已随
        // 最终回答的 trace 以 process_text 落库，单独画会与实时视图割裂。
        if (Array.isArray(m.tool_calls) && m.tool_calls.length) continue;

        const proc = traceToProcess(m.trace, m.stats && m.stats.elapsed_s);
        if (proc) blocks.push(proc);

        const text = typeof m.content === "string" ? m.content : "";
        if (text) blocks.push({ kind: "answer", text: text, mid: m.mid || null, streaming: false });

        if (m.stats) blocks.push({ kind: "meta", elapsed: m.stats.elapsed_s != null ? m.stats.elapsed_s : null, usage: m.stats.usage || null });
        continue;
      }
    }

    return blocks;
  }

  // ---------- 实时追踪器（增量渲染用）----------
  /*
   * 实时路径不能像回放那样「一次性渲染」：它有打字机效果（rAF 合帧）、
   * 气泡就地升级（流式小字 → done 时升级为正文卡）、摘要行秒数跳动等
   * 按时间驱动的行为。若每条 delta 都重跑 blocksFromEvents→renderBlocks，
   * 会每帧重建整个 DOM，打字机与滚动位置全毁。
   *
   * 所以实时路径保留增量 DOM 机制，但把「哪些工具在跑、配对到哪个结果、
   * 什么状态」这类**易错的记账逻辑**收到这里——与 blocksFromEvents 用同一
   * 套配对/状态判定，两路径不再各写一份（bug 温床正在这里）。
   *
   * 用法：每个回合 new 一个；feed(event) 逐条喂事件，返回本次产生的
   * 增量信息（如「刚配对上结果的工具块」），DOM 层据此更新节点。
   */
  function createLiveTracker() {
    let process = null;   // 当前回合的 process 记账
    let lastTool = null;  // 最近一个 running 的工具块

    function ensureProcess() {
      if (!process) process = { kind: "process", steps: 0, items: [] };
      return process;
    }

    function reset() {
      process = null;
      lastTool = null;
    }

    /*
     * 喂一条事件。返回：
     *   null                       该事件与工具记账无关
     *   { kind:"tool_open", tool }  新工具开始跑（DOM 层画调用行）
     *   { kind:"tool_close", tool } 工具拿到结果（DOM 层画结果行/回填徽章）
     *   { kind:"tool_wait", tool }  权限确认（DOM 层画确认卡）
     */
    function feed(evt) {
      if (!isObj(evt)) return null;
      const t = evt.type;

      if (t === "turn_start") {
        reset();
        return null;
      }
      if (t === "round") {
        ensureProcess().items.push({ kind: "note", roundHead: true, text: `🧠 思考 · 第 ${evt.round} 轮` });
        return null;
      }
      if (t === "reasoning_delta") {
        return null;  // 思考流归 DOM 层的 thinkEl，不占步数
      }
      if (t === "tool_call") {
        const tool = {
          kind: "tool",
          name: evt.name || "",
          arguments: evt.arguments != null ? evt.arguments : "{}",
          result: null,
          status: "running",
        };
        ensureProcess().items.push(tool);
        ensureProcess().steps += 1;
        lastTool = tool;
        return { kind: "tool_open", tool: tool };
      }
      if (t === "tool_result") {
        // 与 blocksFromEvents 完全同一套配对规则：优先回填最近一个同名 running
        const items = process ? process.items : [];
        let target = null;
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (it.kind === "tool" && it.status === "running" && (!evt.name || it.name === evt.name)) {
            target = it;
            break;
          }
        }
        if (!target && lastTool && lastTool.status === "running") target = lastTool;
        if (target) {
          target.result = evt.result != null ? evt.result : "";
          target.status = toolStatus(target.result);
        } else {
          target = {
            kind: "tool",
            name: evt.name || "",
            arguments: "{}",
            result: evt.result != null ? evt.result : "",
            status: toolStatus(evt.result),
            orphan: true,
          };
          ensureProcess().items.push(target);
        }
        lastTool = null;
        return { kind: "tool_close", tool: target };
      }
      if (t === "permission_request") {
        // 字段按后端真实事件体：{id, tool, input, reason}（见 agent.py）
        const tool = {
          kind: "tool",
          name: evt.tool || "",
          arguments: evt.input != null
            ? (typeof evt.input === "string" ? evt.input : JSON.stringify(evt.input))
            : "{}",
          result: null,
          status: "waiting",
          permission: { request_id: evt.id || null, reason: evt.reason || "" },
        };
        ensureProcess().items.push(tool);
        return { kind: "tool_wait", tool: tool };
      }
      return null;
    }

    function state() {
      return process;
    }

    return { feed: feed, state: state, reset: reset };
  }

  return {
    blocksFromEvents: blocksFromEvents,
    blocksFromHistory: blocksFromHistory,
    createLiveTracker: createLiveTracker,
    // 导出给测试与渲染层复用
    _toolStatus: toolStatus,
    _parseResult: parseResult,
  };
});
