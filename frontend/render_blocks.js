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
 * 用户「过程说明弱化、可折叠」的偏好一致。唯一例外：进行中回合（running，
 * 切会话/刷新回来的快照）默认展开——用户回来看的正是"现在做到哪了"，
 * 收起只剩一行秒数等于没回放；仍可点 summary 手动收起，代码不再翻回。
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
   *   makeCopyBtn(text) / wrapWithActions(bubble, actions) → 可选；
   *     答案气泡下方的 📋 动作条（不注入则退回裸气泡，测试用）
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

    function renderProcessItem(item, running) {
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
        // 进行中快照的末位工具仍可能是 running：摘要行带🔵徽章提示"还在跑"
        if (running && item.status === "running" && callEl.querySelector) {
          const s = callEl.querySelector("summary");
          if (s) s.textContent = "🔵 " + s.textContent;
        }
        // 子代理嵌套卡（spawn_subagent 专用）：夹在调用行与结果行之间，每个
        // 子任务一张折叠卡，内部条目复用同一渲染器（note/reasoning/tool）。
        if (Array.isArray(item.subtasks) && item.subtasks.length) {
          const wrap = doc.createElement("div");
          wrap.className = "subagent-cards";
          for (const st of item.subtasks) {
            const d = doc.createElement("details");
            d.className = "trace-nested";
            const s = doc.createElement("summary");
            const idx = typeof st.index === "number" ? " " + (st.index + 1) : "";
            s.textContent = "🔍 子任务" + idx + "：" + String(st.task || "").slice(0, 60);
            d.appendChild(s);
            d.appendChild(renderProcessGroup(Array.isArray(st.items) ? st.items : [], running));
            wrap.appendChild(d);
          }
          frag.appendChild(wrap);
        }
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

    // ---- 过程条目按类型归组：思考一窗、说明一卡、其余保持时序 ----
    // 顶层过程块与子代理嵌套卡共用。reasoning 全部收进一个 think-line 固定窗
    // （轮次标题 roundHead 降为窗内分段头），demoted 说明全部收进一张 note-box
    // 卡；工具行/权限卡/系统提醒等仍按原顺序平铺。与实时路径（app.js 整回合
    // 单窗单卡）同一副面孔——思考归思考、说明归说明、调用归调用。
    function renderProcessGroup(items, running) {
      const frag = doc.createDocumentFragment();
      const segs = [];   // 思考分段：{ head, text, live }
      const notes = [];  // 说明文本
      const inline = []; // 保持时序的其余条目
      let pendingHead = null;  // 待定性的轮次标题：跟思考进窗，否则留在时间线
      let curSeg = null;
      for (const it of Array.isArray(items) ? items : []) {
        if (it && it.kind === "note" && it.roundHead) {
          pendingHead = it;  // 等下一个条目定性
          continue;
        }
        if (it && it.kind === "reasoning") {
          const t = it.text || "";
          if (t || curSeg) {
            // 新轮次标题在手（或本就是新段）：开新分段；同轮工具后的思考
            // （无标题）续写当前段——与实时路径"一轮一段"的口径一致
            if (pendingHead || !curSeg) {
              curSeg = { head: pendingHead ? pendingHead.text : null, text: "", live: false };
              segs.push(curSeg);
            }
            curSeg.text += t;
            if (it.live) curSeg.live = true;
            pendingHead = null;
          }
          continue;
        }
        if (it && it.kind === "note" && it.demoted) {
          notes.push(it.text || "");
          continue;  // 轮次标题保持待定：它可能属于时间线上的下一个工具组
        }
        if (pendingHead) { inline.push(pendingHead); pendingHead = null; }
        inline.push(it);
      }
      if (pendingHead) inline.push(pendingHead);  // 尾部悬挂的轮次标题归时间线

      if (segs.length) {
        const box = doc.createElement("div");
        box.className = "think-line";
        for (const s of segs) {
          if (s.head) {
            const h = doc.createElement("div");
            h.className = "think-seg-head";
            h.textContent = s.head;
            box.appendChild(h);
          }
          const b = doc.createElement("div");
          b.className = "think-seg-body" + (running && s.live ? " live" : "");
          b.textContent = s.text;
          box.appendChild(b);
        }
        frag.appendChild(box);
      }
      if (notes.length) {
        const nb = doc.createElement("div");
        nb.className = "note-box demoted";
        // 说明卡排在思考窗下方（.trace 是列向 flex + order，见 style.css）：
        // 思考收敛在上方固定高滚动窗里，说明在它下面展开。说明是给用户看的
        // 内容——可以刷屏、不做 DrainMode 截断：整回合所有过程说明依次列出，
        // 用户展开过程卡就能按顺序读完全部说明。
        for (const t of notes) {
          const n = doc.createElement("div");
          n.className = "process-text";
          n.textContent = t;
          nb.appendChild(n);
        }
        frag.appendChild(nb);
      }
      for (const it of inline) frag.appendChild(renderProcessItem(it, running));
      return frag;
    }

    // ---- 顶层块 ----

    function renderBlock(block) {
      if (block.kind === "user") {
        return deps.railTag(deps.buildUserBubble(block.text, block.atts || []), block.mid, "user");
      }

      if (block.kind === "answer") {
        const el = deps.buildBubble("assistant", block.text);
        if (block.streaming) el.classList.add("streaming");
        // 答案气泡下挂 📋 复制动作条（与用户消息同一套 msg-group 悬停交互）。
        // deps 未注入（Node 测试的假 deps）时退回裸气泡，行为与旧版一致。
        if (deps.makeCopyBtn && deps.wrapWithActions) {
          const actions = doc.createElement("div");
          actions.className = "msg-actions";
          actions.appendChild(deps.makeCopyBtn(block.text));
          return deps.railTag(deps.wrapWithActions(el, actions), block.mid, "assistant");
        }
        return deps.railTag(el, block.mid, "assistant");
      }

      if (block.kind === "process") {
        const d = doc.createElement("details");
        d.className = "trace" + (block.running ? " running" : "");
        // 进行中的回合默认展开（用户切回会话要立刻看到进行到哪了），已定稿的
        // 历史回合保持折叠（正文区只给答案）。两种状态都可手动切换，渲染后
        // 代码不再强制改 open——尤其不能在用户手动收起后翻回展开态。
        d.open = !!block.running;
        // 进行中快照卡打接管标记：实时 SSE 补发到达时，app.js 的 ensureTrace 按
        // 此属性原地认领这张卡继续实时更新（否则时间线尾部会再建一张，快照卡 +
        // 实时卡两张叠着）。原先只有 historyNode 事后打标，但带附件的归档路径
        // 在此之前就 return 了——渲染器自己打标，两条路径都不漏。
        // 标签只是 DOM 数据属性，Node 假 doc 环境用 dataset 兜底，不影响结构测试。
        if (block.running && block.startedAt != null && d.dataset) {
          d.dataset.livetrace = String(block.startedAt);
        }
        d.appendChild(doc.createElement("summary"));
        d.appendChild(renderProcessGroup(block.items, block.running));
        const label = block.steps > 0 ? "已工作" : "已思考";
        // 秒数：已定稿的回合用固定值；进行中的回合（block.running）现算——
        // 它的起点来自服务端（补发/切会话路径），不现算就会显示成 0 秒。
        const live = block.running && block.startedAt
          ? Math.max(0, Date.now() / 1000 - block.startedAt) : null;
        const secs = live != null ? live : block.elapsed;
        const t = secs != null ? ` ${deps.fmtElapsed ? deps.fmtElapsed(secs) : secs + "s"}` : "";
        d.querySelector("summary").textContent = `${label}${t} · ${block.steps} 步`;
        if (live != null) {
          // 生成期间让秒数自己跳动（数据到齐，行为与实时折叠条一致）。
          // 停止条件除了节点被移除，还要看 running 类：回合定稿/出错时 app.js 只
          // 摘 running 不移除节点（用户还要点开回看过程），本计时器必须跟着停，
          // 否则已结束的回合秒数永远在涨（步数还是渲染时的冻结快照，越走越假）。
          const timer = setInterval(() => {
            if (!d.isConnected || !d.classList.contains("running")) { clearInterval(timer); return; }
            d.querySelector("summary").textContent =
              `${label} ${deps.fmtElapsed ? deps.fmtElapsed(Math.max(0, Date.now() / 1000 - block.startedAt)) : Math.round(Date.now() / 1000 - block.startedAt) + "s"} · ${block.steps} 步`;
          }, 200);
        }
        return d;
      }

      // kind === "todo" 已移除：任务清单改为右上角 📋 浮窗展示（app.js），
      // 对话流不再渲染清单卡；旧历史里残留的 todo 块直接跳过。

      if (block.kind === "compact") return deps.compactCard(block.summary);

      if (block.kind === "meta") {
        const div = doc.createElement("div");
        div.className = "meta";
        div.textContent = deps.metaText(block.elapsed, block.usage);
        return div;
      }

      if (block.kind === "error") {
        const eb = deps.buildBubble("error", block.text);
        // retryable（余额/限流/网络类错误）在回放路径同样给"重试上一条"：
        // 与实时 error 事件的语义一致（app.js），刷新后仍可一键重发。
        if (block.retryable && deps.makeRetryButton) {
          const holder = doc.createElement("div");
          holder.appendChild(eb);
          holder.appendChild(deps.makeRetryButton());
          return holder;
        }
        return eb;
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
