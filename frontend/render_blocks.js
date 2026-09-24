/*
 * renderBlocks：把 blocks.js 产出的 Block[] 画成 DOM。
 * =====================================================================
 *
 * 这是二期「单一渲染器」的渲染半边：实时流与历史回放都先经 blocks.js
 * 归一化成同构的 Block[]，再交给本函数画——同一语义只有这一处实现。
 *
 * 设计约束：
 *  - 不重复实现已有零件（气泡、工具行、diff、统计行…）：这些通过 deps
 *    注入进来（页面里传 app.js 的现成函数），保持行为逐字节一致；
 *  - 不依赖浏览器专有 API 之外的东西，便于在 Node 里用假 deps 做结构测试；
 *  - 一个 Block[] → 一个 DocumentFragment（挂进 #chat 时不留包装层，
 *    否则 .bubble.user 的 align-self 会因父级不是 flex 而失效）。
 *
 * 过程块（process）默认折叠：正文区只给答案，点开才看执行痕迹——这与
 * 用户「过程说明弱化、可折叠」的偏好一致。
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CodingAgentRenderBlocks = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 工具状态 → 摘要行前缀徽章。waiting/running 用动态样式，其余用文字。
  // 说明：本项目工具串行，同时最多一个 running。
  const STATUS_BADGE = {
    running: "🔵",
    waiting: "🛡",
    ok: "✅",
    err: "❌",
    denied: "🚫",
  };

  /*
   * deps 需要提供（页面里从 app.js 注入）：
   *   doc()                    → document（测试可传假对象）
   *   buildBubble(cls, text)   → 气泡节点（assistant 走 Markdown）
   *   buildUserBubble(t, atts) → 带附件的用户气泡
   *   railTag(node, mid, role) → 打上 mid/role 锚点（导航条定位用）
   *   makeToolCallLine(name, argsStr)
   *   makeToolResultLine(name, resultStr)
   *   decorateWriteCard(callEl, resultStr)
   *   metaText(elapsed, usage)
   *   compactCard(summary)
   *   artifactCard(m)          → 外置归档消息卡（可选）
   */
  function createRenderer(deps) {
    const doc = deps.doc;

    // ---- 单个子块（process 内部）----

    function renderProcessItem(item) {
      if (item.kind === "note") {
        const div = doc.createElement("div");
        if (item.roundHead) {
          // 轮次标题：与旧 traceFromHistory 的 trace-line 一致
          div.className = "trace-line";
          div.textContent = item.wrapUp
            ? item.text.replace(" 轮", " 轮（收尾）")
            : item.text;
        } else if (item.reminder) {
          div.className = "trace-line";
          div.textContent = item.text;
        } else {
          // 过程性正文：demoted 带「💬 说明」标记，防冒充上方思考流
          div.className = "process-text" + (item.demoted ? " demoted" : "");
          div.textContent = item.text;
        }
        return div;
      }

      if (item.kind === "reasoning") {
        const div = doc.createElement("div");
        div.className = "think-line";
        div.textContent = item.text;
        return div;
      }

      if (item.kind === "tool") {
        // 写入类调用卡需要等结果回填徽章：这里按「先调用行、后结果行」配对，
        // 与旧 traceFromHistory 的顺序一致。
        const callEl = deps.makeToolCallLine(item.name, item.arguments || "{}");
        const frag = doc.createDocumentFragment();
        frag.appendChild(callEl);
        if (item.status !== "running" && item.status !== "waiting" && item.result != null) {
          if (callEl.classList && callEl.classList.contains("card")) {
            deps.decorateWriteCard(callEl, item.result);
          }
          frag.appendChild(deps.makeToolResultLine(item.name, item.result));
        }
        return frag;
      }

      return doc.createDocumentFragment();
    }

    // ---- 顶层块 ----

    function renderBlock(block) {
      if (block.kind === "user") {
        return deps.railTag(deps.buildUserBubble(block.text, block.atts || []), block.mid, "user");
      }

      if (block.kind === "answer") {
        const el = deps.buildBubble("assistant", block.text);
        if (block.streaming) el.classList.add("streaming");
        return deps.railTag(el, block.mid, "assistant");
      }

      if (block.kind === "process") {
        const d = doc.createElement("details");
        d.className = "trace" + (block.running ? " running" : "");
        d.open = false;
        d.appendChild(doc.createElement("summary"));
        for (const it of block.items) d.appendChild(renderProcessItem(it));
        const label = block.steps > 0 ? "已工作" : "已思考";
        // 秒数：已定稿的回合用固定值；进行中的回合（block.running）现算——
        // 它的起点来自服务端（补发/切会话路径），不现算就会显示成 0 秒。
        const live = block.running && block.startedAt
          ? Math.max(0, Date.now() / 1000 - block.startedAt) : null;
        const secs = live != null ? live : block.elapsed;
        const t = secs != null ? ` ${deps.fmtElapsed ? deps.fmtElapsed(secs) : secs + "s"}` : "";
        d.querySelector("summary").textContent = `${label}${t} · ${block.steps} 步`;
        if (live != null) {
          // 生成期间让秒数自己跳动（数据到齐，行为与实时折叠条一致）
          const timer = setInterval(() => {
            if (!d.isConnected) { clearInterval(timer); return; }
            d.querySelector("summary").textContent =
              `${label} ${deps.fmtElapsed ? deps.fmtElapsed(Math.max(0, Date.now() / 1000 - block.startedAt)) : Math.round(Date.now() / 1000 - block.startedAt) + "s"} · ${block.steps} 步`;
          }, 200);
        }
        return d;
      }

      if (block.kind === "compact") return deps.compactCard(block.summary);

      if (block.kind === "meta") {
        const div = doc.createElement("div");
        div.className = "meta";
        div.textContent = deps.metaText(block.elapsed, block.usage);
        return div;
      }

      if (block.kind === "error") {
        return deps.buildBubble("error", block.text);
      }

      return doc.createDocumentFragment();
    }

    /*
     * 渲染整批块。返回 DocumentFragment，可直接 appendChild 进 #chat。
     * opts.artifactCard：外置归档消息（历史路径专用，块里没这种 kind）。
     */
    function renderBlocks(blocks) {
      const frag = doc.createDocumentFragment();
      for (const b of Array.isArray(blocks) ? blocks : []) {
        const node = renderBlock(b);
        if (node) frag.appendChild(node);
      }
      return frag;
    }

    return { renderBlocks, renderBlock, renderProcessItem, STATUS_BADGE };
  }

  return { createRenderer, STATUS_BADGE };
});
