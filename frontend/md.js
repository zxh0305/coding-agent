// Markdown 渲染引擎（marked + DOMPurify + highlight.js）
// =====================================================
// 三个库 vendor 在 frontend/vendor/（无构建、改动直达的项目约定不变）：
//   * marked 把模型输出的 markdown 解析成 HTML（GFM 表格/嵌套列表/删除线，
//     手搓版覆盖不了的语法从此全部正确）；
//   * DOMPurify 对产出 HTML 消毒——模型输出是不可信内容，marked 的 HTML 里
//     若混入 <script>/onerror=/javascript: 一律剥掉，这是安全闸，绝不可省；
//   * highlight.js 给代码块上色（github-dark 主题，配合 --code-dark 深底）。
// 流式期间仍走 textContent 快刷（rAF 批量），定稿才走本渲染——与思考流不解析
// markdown 是同一条性能决策：字数越多越卡的根源就是流式反复全量解析。
//
// 兜底：三个库任意缺失（如被人挪走了 vendor/ 目录）时整段按纯文本渲染，
// 绝不退回旧的手搓解析器——半吊子解析是最坏状态。

// ---------- Markdown 渲染引擎（marked + DOMPurify + highlight.js） ----------
// 三个库 vendor 在 frontend/vendor/（无构建、改动直达的项目约定不变）：
//   * marked 把模型输出的 markdown 解析成 HTML（GFM 表格/嵌套列表/删除线，
//     手搓版覆盖不了的语法从此全部正确）；
//   * DOMPurify 对产出 HTML 消毒——模型输出是不可信内容，marked 的 HTML 里
//     若混入 <script>/onerror=/javascript: 一律剥掉，这是安全闸，绝不可省；
//   * highlight.js 给代码块上色（github-dark 主题，配合 --code-dark 深底）。
// 流式期间仍走 textContent 快刷（rAF 批量），定稿才走本渲染——与思考流不解析
// markdown 是同一条性能决策：字数越多越卡的根源就是流式反复全量解析。
//
// 兜底：三个库任意缺失（如被人挪走了 vendor/ 目录）时整段按纯文本渲染，
// 绝不退回旧的手搓解析器——半吊子解析是最坏状态。
const MD_READY = typeof window.marked?.parse === "function"
  && typeof window.DOMPurify?.sanitize === "function";

// 消毒白名单：只留纯 HTML 展示面。style 属性整体剥掉（防样式注入破坏版式，
// ZCode 设计规范同样禁任意内联字号/颜色）；class 保留（hljs 高亮靠它）。
function sanitizeMarkdownHtml(html) {
  return window.DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed"],
    FORBID_ATTR: ["style", "srcset"],
  });
}

// 把 marked 的标准标签回填成本项目既有的 .md-* 类名——.bubble.md 与
// .docs-view 两套排版 CSS（间距/字号/表格滚动）原样复用，一行都不用改。
function decorateMarkdown(root) {
  root.querySelectorAll("p").forEach((el) => el.classList.add("md-p"));
  root.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((el) => {
    // h5/h6 归并到 md-h4 的字号层级（旧渲染器同规则），标签本身不变
    el.classList.add("md-h", "md-h" + Math.min(Number(el.tagName[1]), 4));
  });
  root.querySelectorAll("blockquote").forEach((el) => el.classList.add("md-quote"));
  root.querySelectorAll("hr").forEach((el) => el.classList.add("md-hr"));
  root.querySelectorAll("ul,ol").forEach((el) => el.classList.add("md-list"));
  root.querySelectorAll("table").forEach((el) => {
    el.classList.add("md-table");
    const wrap = document.createElement("div");
    wrap.className = "md-table-wrap";
    el.replaceWith(wrap);
    wrap.appendChild(el);  // 列多时横向滚动，不撑破气泡
  });
  root.querySelectorAll("pre").forEach((pre) => {
    pre.classList.add("md-pre");
    const code = pre.querySelector("code");
    const lang = code ? (code.className.match(/language-([\w+#.-]+)/) || [])[1] : null;
    if (lang) {  // 语言角标（与旧渲染器的 .md-lang 同样式）
      const tag = document.createElement("span");
      tag.className = "md-lang";
      tag.textContent = lang;
      pre.insertBefore(tag, pre.firstChild);
    }
  });
  root.querySelectorAll("a").forEach((a) => {
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  });
  return root;
}

function renderMarkdown(src) {
  const root = document.createDocumentFragment();
  const text = String(src || "");
  if (!text) return root;
  if (MD_READY) {
    let html = "";
    try {
      html = window.marked.parse(text, { gfm: true, breaks: true, async: false });
    } catch { /* 解析失败按纯文本兜底 */ }
    if (html) {
      const tpl = document.createElement("template");
      tpl.innerHTML = sanitizeMarkdownHtml(html);
      decorateMarkdown(tpl.content);
      if (window.hljs) {
        tpl.content.querySelectorAll("pre code").forEach((code) => {
          try { window.hljs.highlightElement(code); } catch { /* 高亮失败不致命 */ }
        });
      }
      root.appendChild(tpl.content);
      return root;
    }
  }
  const p = document.createElement("p");
  p.className = "md-p";
  p.textContent = text;
  root.appendChild(p);
  return root;
}
