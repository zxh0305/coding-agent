/*
 * 记忆面板的纯逻辑（UMD，可被 node --test 直接 require）
 * =====================================================================
 *
 * 只放不碰 DOM 的决策函数：时间格式化、条目分组/排序、文件名校验预检、
 * 层切换时的文案。DOM 部分留在 app.js。
 *
 * 为什么单独成文件：app.js 整文件顶层执行、碰 document，不能被 require，
 * 于是它的逻辑天然无法单测（记忆面板的大部分 bug 恰好都在"状态→展示"这一步：
 * 无 frontmatter 的老记忆显示成空白、换层后计数不刷新、非法文件名发出去才报错）。
 * 拆出纯函数是让这部分可测的最小代价，套路与 queue_state.js 一致。
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MemoryView = factory();
})(typeof self !== "undefined" ? self : this, function () {

  // 记忆文件名白名单，与后端 memory.NAME_RE 保持一致：
  // ^[a-z0-9][a-z0-9-]*\.md$（小写字母/数字/短横线，必须以 .md 结尾）
  const NAME_RE = /^[a-z0-9][a-z0-9-]*\.md$/;

  /** 前端预检文件名，返回 null 表示合法，否则返回人能读懂的原因。
   *  后端仍会再校验一次——这里只为把错误挡在发出请求之前，少一次往返。 */
  function checkName(raw) {
    const name = String(raw || "").trim();
    if (!name) return "文件名不能为空";
    if (!name.endsWith(".md")) return "文件名必须以 .md 结尾";
    if (!NAME_RE.test(name)) {
      return "只能用小写字母、数字、短横线（如 pnpm-not-npm.md）：不支持大写、下划线、中文与斜杠";
    }
    return null;
  }

  /** 把后端 mtime（秒）渲染成"多久前"。memories 是低频、跨越数月的资产，
   *  相对时间是它最有用的时间表达。 */
  function relTime(mtime, nowSec) {
    if (!mtime) return "";
    const now = typeof nowSec === "number" ? nowSec : Date.now() / 1000;
    const d = Math.max(0, now - mtime);
    if (d < 60) return "刚刚";
    if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
    const dt = new Date(mtime * 1000);
    return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  }

  /** 一层的说明文案：让用户明白这一层意味着什么、写错会怎样。 */
  function layerDesc(layer) {
    if (layer === "personal") {
      return "关于你这个人——回复风格、工具偏好、工作习惯。换任何项目目录都生效。";
    }
    return "只属于当前会话绑定的项目：构建/测试命令、架构决策、本仓库的坑。";
  }

  /** 项目层是否可用：它依赖当前会话的工作区；没有会话就没有项目记忆。 */
  function projectLayerAvailable(sessionId) {
    return !!(sessionId && String(sessionId).trim());
  }

  /** 条目 → 展示模型。关键是把"没有 frontmatter 的老记忆"如实标出来：
   *  真实数据里 33/74 条是模型当年直接写文件留下的，解析不出描述——
   *  显示成空白会让人以为坏了，标记为"未整理"才诚实且可操作。 */
  function decorate(entry) {
    const has = !!entry.has_frontmatter;
    return {
      file: entry.file,
      desc: entry.description || (has ? "(无描述)" : "(未整理：无描述)"),
      type: entry.type || "",
      owner: entry.owner || "",
      updated: entry.updated || "",
      when: relTime(entry.mtime),
      bytes: entry.bytes || 0,
      sensitive: !!entry.sensitive,
      uncurated: !has,
      excerpt: entry.excerpt || "",
      // 层归属只认 owner 字段：没有它就是项目层（迁移时没写 owner，
      // 全部按项目层处理——这与后端 _op_target_dir 的默认一致）。
      isPersonal: entry.owner === "user",
    };
  }

  /** 两层的计数，用于 tab 上的角标。 */
  function counts(personal, project) {
    return {
      personal: Array.isArray(personal) ? personal.length : 0,
      project: Array.isArray(project) ? project.length : 0,
    };
  }

  /** 换层时目标层该给的 owner 值。 */
  function ownerForLayer(layer) {
    return layer === "personal" ? "user" : null;
  }

  return { checkName, relTime, layerDesc, projectLayerAvailable,
           decorate, counts, ownerForLayer, NAME_RE };
});
