/* Local preview annotations. Source attachments are never modified or uploaded. */
window.ImageEditor = (() => {
  function open({src, gallery, index = 0, notify = () => {}}) {
    const sources = Array.isArray(gallery) && gallery.length ? gallery : [src];
    let current = Math.min(sources.length - 1, Math.max(0, index));
    let tool = "select", zoom = 1, image = null, ready = false, closed = false;
    let gesture = null, draft = null, textInput = null, generation = 0, copying = false;
    const states = new Map();
    const state = () => {
      if (!states.has(current)) states.set(current, {marks:[], undo:[], redo:[]});
      return states.get(current);
    };
    const clone = value => JSON.parse(JSON.stringify(value));
    const box = document.createElement("div");
    box.className = "lightbox image-editor";
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "图片预览与标注");
    const previousFocus = document.activeElement;
    const viewport = document.createElement("div"); viewport.className = "lb-viewport";
    const stage = document.createElement("div"); stage.className = "lb-stage";
    const canvas = document.createElement("canvas"); canvas.className = "lb-canvas";
    canvas.setAttribute("aria-label", "图片标注画布");
    const context = canvas.getContext("2d");
    const status = document.createElement("div"); status.className = "lb-status";
    status.setAttribute("role", "status");
    stage.append(canvas); viewport.append(stage);
    const toolbar = document.createElement("div"); toolbar.className = "lb-toolbar";
    toolbar.setAttribute("role", "toolbar"); toolbar.setAttribute("aria-label", "图片工具");
    const buttons = new Map();
    function button(label, symbol, action, key) {
      const el = document.createElement("button"); el.type = "button";
      el.title = label; el.setAttribute("aria-label", label); el.textContent = symbol;
      el.addEventListener("click", action); toolbar.append(el);
      if (key) { el.dataset.action = key; buttons.set(key, el); }
      return el;
    }
    function separator() { const line = document.createElement("hr"); toolbar.append(line); }
    function checkpoint() {
      const value = state(); value.undo.push(clone(value.marks));
      if (value.undo.length > 30) value.undo.shift();
      value.redo = [];
      trimHistory();
    }
    function trimHistory() {
      const value = state();
      while (value.undo.length && JSON.stringify([value.undo, value.redo]).length > 2000000) value.undo.shift();
      while (value.redo.length && JSON.stringify(value.redo).length > 2000000) value.redo.shift();
    }
    function sync() {
      for (const [key, el] of buttons) {
        if (["select","pen","text","rect","ellipse","arrow","erase"].includes(key)) {
          el.setAttribute("aria-pressed", String(tool === key));
        }
      }
      buttons.get("undo").disabled = !ready || !state().undo.length;
      buttons.get("redo").disabled = !ready || !state().redo.length;
      buttons.get("copy").disabled = !ready || copying;
      buttons.get("download").disabled = !ready;
      canvas.style.cursor = tool === "select" ? "grab" : tool === "text" ? "text" : "crosshair";
    }
    function path(mark) {
      context.beginPath();
      if (mark.type === "pen") {
        context.moveTo(mark.points[0].x, mark.points[0].y);
        for (const point of mark.points.slice(1)) context.lineTo(point.x, point.y);
        if (mark.points.length === 1) context.lineTo(mark.points[0].x + .1, mark.points[0].y + .1);
      } else if (mark.type === "rect") {
        context.rect(mark.x, mark.y, mark.end.x - mark.x, mark.end.y - mark.y);
      } else if (mark.type === "ellipse") {
        context.ellipse((mark.x + mark.end.x) / 2, (mark.y + mark.end.y) / 2,
          Math.abs(mark.end.x - mark.x) / 2, Math.abs(mark.end.y - mark.y) / 2, 0, 0, Math.PI * 2);
      } else if (mark.type === "arrow") {
        const angle = Math.atan2(mark.end.y - mark.y, mark.end.x - mark.x), length = mark.width * 4 + 10;
        context.moveTo(mark.x, mark.y); context.lineTo(mark.end.x, mark.end.y);
        context.moveTo(mark.end.x - length * Math.cos(angle - .5), mark.end.y - length * Math.sin(angle - .5));
        context.lineTo(mark.end.x, mark.end.y);
        context.lineTo(mark.end.x - length * Math.cos(angle + .5), mark.end.y - length * Math.sin(angle + .5));
      }
    }
    function drawMark(mark) {
      context.strokeStyle = mark.color; context.fillStyle = mark.color;
      context.lineWidth = mark.width; context.lineCap = "round"; context.lineJoin = "round";
      if (mark.type === "text") {
        context.font = `${mark.size}px sans-serif`; context.textBaseline = "top";
        context.fillText(mark.text, mark.x, mark.y);
      } else { path(mark); context.stroke(); }
    }
    function draw() {
      if (!ready) return;
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      for (const mark of state().marks) drawMark(mark);
      if (draft) drawMark(draft);
      sync();
    }
    function layout() {
      if (!ready) return;
      const fit = Math.min((viewport.clientWidth - 24) / canvas.width, (viewport.clientHeight - 24) / canvas.height, 1);
      const width = Math.max(1, canvas.width * fit * zoom), height = Math.max(1, canvas.height * fit * zoom);
      canvas.style.width = width + "px"; canvas.style.height = height + "px";
      stage.style.width = Math.max(viewport.clientWidth, width + 24) + "px";
      stage.style.height = Math.max(viewport.clientHeight, height + 24) + "px";
      buttons.get("reset").textContent = Math.round(zoom * 100) + "%";
    }
    function point(event) {
      const rect = canvas.getBoundingClientRect();
      return {x:Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * canvas.width / rect.width)),
              y:Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * canvas.height / rect.height))};
    }
    function finishText(commit = true) {
      if (!textInput) return;
      const {input, mark} = textInput; textInput = null;
      const text = input.value.trim(); input.remove();
      if (commit && text && state().marks.length < 300) {
        checkpoint(); state().marks.push({...mark, text}); draw();
      }
    }
    function cancelGesture() {
      if (gesture?.before) state().marks = gesture.before;
      gesture = null; draft = null; draw();
    }
    function setTool(value) { finishText(); cancelGesture(); tool = value; sync(); }
    function eraseAt(pos) {
      const marks = state().marks;
      for (let i = marks.length - 1; i >= 0; i--) {
        const mark = marks[i]; let hit = false;
        if (mark.type === "text") {
          context.font = `${mark.size}px sans-serif`;
          hit = pos.x >= mark.x && pos.x <= mark.x + context.measureText(mark.text).width && pos.y >= mark.y && pos.y <= mark.y + mark.size;
        } else {
          path(mark); context.lineWidth = mark.width + 16;
          hit = context.isPointInStroke(pos.x, pos.y);
        }
        if (hit) { marks.splice(i, 1); return; }
      }
    }
    canvas.addEventListener("pointerdown", event => {
      if (!ready || event.button !== 0 || gesture) return;
      event.preventDefault(); finishText();
      const pos = point(event);
      if (tool === "text") {
        if (state().marks.length >= 300) { notify("标注较多，请先复制或下载后再继续"); return; }
        const input = document.createElement("input"); input.type = "text"; input.maxLength = 300;
        input.className = "lb-text-input"; input.placeholder = "输入文字，回车确认";
        input.setAttribute("aria-label", "标注文字");
        input.style.left = Math.max(8, Math.min(event.clientX, innerWidth - 240)) + "px";
        input.style.top = Math.max(8, Math.min(event.clientY, innerHeight - 48)) + "px";
        textInput = {input, mark:{type:"text", ...pos, color:color.value, width:+width.value, size:Math.max(24, +width.value * 6)}};
        input.addEventListener("keydown", e => {
          if (e.isComposing) return;
          if (e.key === "Enter") { e.preventDefault(); finishText(); }
          if (e.key === "Escape") { e.stopPropagation(); finishText(false); }
        });
        input.addEventListener("blur", () => finishText()); box.append(input); input.focus(); return;
      }
      if (tool !== "select" && tool !== "erase" && state().marks.length >= 300) {
        notify("标注较多，请先复制或下载后再继续"); return;
      }
      if (tool === "pen" && state().marks.reduce((n, mark) => n + (mark.points?.length || 0), 0) > 19990) {
        notify("画笔标注较多，请先复制或下载后再继续"); return;
      }
      canvas.setPointerCapture(event.pointerId);
      gesture = {id:event.pointerId, startX:event.clientX, startY:event.clientY,
                 left:viewport.scrollLeft, top:viewport.scrollTop, before:clone(state().marks)};
      if (tool === "erase") eraseAt(pos);
      else if (tool !== "select") draft = {type:tool, ...pos, end:pos, points:[pos], color:color.value, width:+width.value};
      draw();
    });
    canvas.addEventListener("pointermove", event => {
      if (!gesture || gesture.id !== event.pointerId) return;
      if (tool === "select") {
        viewport.scrollLeft = gesture.left - (event.clientX - gesture.startX);
        viewport.scrollTop = gesture.top - (event.clientY - gesture.startY); return;
      }
      const pos = point(event);
      if (tool === "erase") eraseAt(pos);
      else if (draft) {
        draft.end = pos;
        if (draft.type === "pen") {
          const limit = Math.min(2000, 20000 - state().marks.reduce((n, mark) => n + (mark.points?.length || 0), 0));
          if (draft.points.length >= limit) draft.points = draft.points.filter((_, index) => index % 2 === 0);
          draft.points.push(pos);
        }
      }
      draw();
    });
    canvas.addEventListener("pointerup", event => {
      if (!gesture || gesture.id !== event.pointerId) return;
      const before = gesture.before;
      if (draft) state().marks.push(draft);
      if (JSON.stringify(before) !== JSON.stringify(state().marks)) {
        state().undo.push(before); if (state().undo.length > 30) state().undo.shift(); state().redo = [];
        trimHistory();
      }
      gesture = null; draft = null; draw();
    });
    canvas.addEventListener("pointercancel", cancelGesture);
    canvas.addEventListener("lostpointercapture", () => { if (gesture) cancelGesture(); });
    function changeZoom(value) { finishText(); cancelGesture(); zoom = Math.max(.25, Math.min(5, value)); layout(); }
    function history(direction) {
      finishText(); cancelGesture(); const value = state(), from = value[direction];
      if (!from.length) return;
      value[direction === "undo" ? "redo" : "undo"].push(clone(value.marks));
      value.marks = from.pop(); draw();
    }
    function blob() {
      finishText(); cancelGesture();
      return new Promise((resolve, reject) => {
        try { canvas.toBlob(value => value ? resolve(value) : reject(new Error("图片导出失败")), "image/png"); }
        catch (error) { reject(error); }
      });
    }
    async function copy() {
      if (!ready) return;
      copying = true; sync();
      try {
        if (!navigator.clipboard?.write || !window.ClipboardItem) throw new Error("clipboard unavailable");
        await navigator.clipboard.write([new ClipboardItem({"image/png":blob()})]);
        if (!closed) notify("已复制图片（含标注）", "success");
      } catch (error) { if (!closed) notify("复制图片失败，可使用右侧下载按钮保存", "error"); }
      finally { copying = false; if (!closed) sync(); }
    }
    async function download() {
      if (!ready) return;
      const filename = `图片标注-${current + 1}.png`;
      try {
        const data = await blob(); if (closed) return;
        const url = URL.createObjectURL(data), link = document.createElement("a");
        link.href = url; link.download = filename; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { if (!closed) notify("图片导出失败，请确认图片可以正常加载", "error"); }
    }
    function load(index) {
      finishText(); cancelGesture(); current = index; ready = false; zoom = 1;
      canvas.hidden = true; status.textContent = "正在加载图片…"; sync();
      const token = ++generation, next = new Image();
      next.onload = () => {
        if (closed || token !== generation) return;
        const ratio = Math.min(1, Math.sqrt(16000000 / (next.naturalWidth * next.naturalHeight)));
        canvas.width = Math.max(1, Math.round(next.naturalWidth * ratio));
        canvas.height = Math.max(1, Math.round(next.naturalHeight * ratio));
        image = next; ready = true; canvas.hidden = false;
        status.textContent = `${current + 1} / ${sources.length} · 标注仅用于复制或下载，原图不变${ratio < 1 ? " · 大图已缩小导出" : ""}`;
        draw(); layout(); viewport.scrollTo(0, 0);
      };
      next.onerror = () => { if (!closed && token === generation) status.textContent = "图片加载失败，请关闭后重试"; };
      next.src = sources[current];
    }
    function step(delta) { if (sources.length > 1) load((current + delta + sources.length) % sources.length); }
    button("关闭图片预览", "×", close, "close");
    if (sources.length > 1) {
      button("上一张图片", "‹", () => step(-1)); button("下一张图片", "›", () => step(1)); separator();
    }
    for (const [key, label, icon] of [["select","移动图片","↖"],["pen","画笔","✎"],["text","文字","T"],
      ["rect","方框","□"],["ellipse","圆形","○"],["arrow","箭头","↗"],["erase","擦除标注（保留原图）","⌫"]]) {
      button(label, icon, () => setTool(key), key);
    }
    const color = document.createElement("input"); color.type = "color"; color.value = "#ef4444";
    color.title = "标注颜色"; color.setAttribute("aria-label", "标注颜色"); toolbar.append(color);
    const width = document.createElement("select"); width.title = "线条粗细"; width.setAttribute("aria-label", "线条粗细");
    for (const size of [2,4,8]) { const option = document.createElement("option"); option.value = size; option.textContent = size + "px"; width.append(option); }
    width.value = "4"; toolbar.append(width); separator();
    button("撤销标注", "↶", () => history("undo"), "undo");
    button("重做标注", "↷", () => history("redo"), "redo"); separator();
    button("复制图片（含标注）", "⧉", copy, "copy");
    button("下载图片（含标注）", "↓", download, "download"); separator();
    button("放大图片", "+", () => changeZoom(zoom * 1.25));
    button("恢复适合窗口大小", "100%", () => changeZoom(1), "reset");
    button("缩小图片", "−", () => changeZoom(zoom / 1.25));
    function keys(event) {
      if (event.key === "Tab") {
        const focusable = [...box.querySelectorAll("button:not(:disabled),input,select")];
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        return;
      }
      if (event.target.matches("input,textarea,select") || event.isComposing) return;
      const key = event.key;
      if (!["Escape","ArrowLeft","ArrowRight","+","=","-","0"].includes(key) &&
          !((event.ctrlKey || event.metaKey) && key.toLowerCase() === "z")) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (key === "Escape") close();
      else if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === "z") history(event.shiftKey ? "redo" : "undo");
      else if (key === "ArrowLeft") step(-1);
      else if (key === "ArrowRight") step(1);
      else if (["+","="].includes(key)) changeZoom(zoom * 1.25);
      else if (key === "-") changeZoom(zoom / 1.25);
      else if (key === "0") changeZoom(1);
    }
    viewport.addEventListener("wheel", event => {
      event.preventDefault(); changeZoom(zoom * (event.deltaY < 0 ? 1.12 : .89));
    }, {passive:false});
    viewport.addEventListener("click", event => { if (event.target === stage || event.target === viewport) close(); });
    box.append(viewport, toolbar, status); document.body.append(box);
    const observer = new ResizeObserver(layout); observer.observe(viewport);
    document.addEventListener("keydown", keys, true);
    // Keep the background composer from receiving modal keyboard shortcuts.
    const layoutRoot = document.getElementById("layout"), wasInert = layoutRoot?.inert;
    if (layoutRoot) layoutRoot.inert = true;
    buttons.get("close").focus(); load(current);
    function close() {
      if (closed) return;
      closed = true; generation++; finishText(false); observer.disconnect();
      document.removeEventListener("keydown", keys, true); box.remove();
      if (layoutRoot) layoutRoot.inert = wasInert;
      if (previousFocus?.isConnected) previousFocus.focus({preventScroll:true});
      states.clear(); image = null; canvas.width = canvas.height = 1;
    }
    return close;
  }
  return {open};
})();
