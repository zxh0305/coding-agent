/*
 * frontend/memory_view.js 的单元测试（Node 原生 test runner，零依赖）
 * 运行：node --test frontend/memory_view.test.mjs
 *
 * 记忆面板的 bug 大多在"状态 → 展示"这一步：无 frontmatter 的老记忆显示成
 * 空白、非法文件名发出去才报错、换层后看不见归属。这些都不需要 DOM，
 * 拆成纯函数就能锁住。
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const M = require("./memory_view.js");

// ---------- 文件名校验（与后端 memory.NAME_RE 同一条规则） ----------

test("合法文件名通过", () => {
  for (const n of ["a.md", "pnpm-not-npm.md", "9lives.md", "a-b-c.md"]) {
    assert.equal(M.checkName(n), null, n);
  }
});

test("非法文件名被拦下并给出人能读懂的说明", () => {
  assert.match(M.checkName(""), /不能为空/);
  assert.match(M.checkName("x.txt"), /\.md/);
  // 大写 / 下划线 / 中文 / 斜杠都不是合法字符——后端会拒，前端先拦省一次往返
  for (const n of ["Up.md", "a_b.md", "中文.md", "../x.md", "a/b.md"]) {
    assert.match(M.checkName(n), /小写字母/, n);
  }
});

test("校验规则与后端白名单一致（大写/下划线必须两边都拒）", () => {
  // 这条用例的意义是：前端预检若比后端松，用户会先看到成功提示再收到 400
  assert.equal(M.NAME_RE.test("Up.md"), false);
  assert.equal(M.NAME_RE.test("a_b.md"), false);
  assert.equal(M.NAME_RE.test("ok-name.md"), true);
});

// ---------- 时间 ----------

test("相对时间按量级分档", () => {
  const now = 1_700_000_000;
  assert.equal(M.relTime(now, now), "刚刚");
  assert.equal(M.relTime(now - 120, now), "2 分钟前");
  assert.equal(M.relTime(now - 3600 * 3, now), "3 小时前");
  assert.equal(M.relTime(now - 86400 * 5, now), "5 天前");
});

test("超过一个月显示绝对日期", () => {
  const now = 1_700_000_000;
  const out = M.relTime(now - 86400 * 90, now);
  assert.match(out, /^\d{4}-\d{2}-\d{2}$/, out);
});

test("没有 mtime 时返回空串而不是 NaN", () => {
  assert.equal(M.relTime(0, 1_700_000_000), "");
  assert.equal(M.relTime(null), "");
});

// ---------- 条目装饰 ----------

test("有 frontmatter 的条目正常显示", () => {
  const d = M.decorate({ file: "a.md", description: "用 pnpm", type: "user",
                         owner: "user", mtime: 1_700_000_000, has_frontmatter: true });
  assert.equal(d.desc, "用 pnpm");
  assert.equal(d.isPersonal, true);
  assert.equal(d.uncurated, false);
});

test("无 frontmatter 的老记忆必须被标为未整理", () => {
  // 真实数据 33/74 条如此（模型当年直接写文件留下的）——显示成空白会让人
  // 以为数据坏了，标成"未整理"才诚实且可操作。
  const d = M.decorate({ file: "legacy.md", mtime: 1_700_000_000,
                         has_frontmatter: false, type: "", description: "" });
  assert.equal(d.uncurated, true);
  assert.match(d.desc, /未整理/);
});

test("缺 description 但有 frontmatter：显示无描述而非未整理", () => {
  const d = M.decorate({ file: "a.md", has_frontmatter: true, mtime: 1 });
  assert.equal(d.uncurated, false);
  assert.equal(d.desc, "(无描述)");
});

test("层归属只认 owner 字段：没有 owner 即为项目层", () => {
  // 与后端 _op_target_dir 的默认一致——迁移时没写 owner，全部按项目层处理
  assert.equal(M.decorate({ file: "a.md", mtime: 1, has_frontmatter: true }).isPersonal, false);
  assert.equal(M.decorate({ file: "a.md", mtime: 1, owner: "user", has_frontmatter: true }).isPersonal, true);
  assert.equal(M.decorate({ file: "a.md", mtime: 1, owner: "project", has_frontmatter: true }).isPersonal, false);
});

test("敏感标记透传到展示层", () => {
  const d = M.decorate({ file: "c.md", mtime: 1, has_frontmatter: true, sensitive: true });
  assert.equal(d.sensitive, true);
});

test("畸形条目不崩", () => {
  for (const e of [{}, { file: "a.md" }, { file: "a.md", mtime: null }]) {
    assert.ok(M.decorate(e), JSON.stringify(e));
  }
});

// ---------- 计数与层 ----------

test("计数忽略非数组输入", () => {
  assert.deepEqual(M.counts(null, undefined), { personal: 0, project: 0 });
  assert.deepEqual(M.counts([1, 2], [1]), { personal: 2, project: 1 });
});

test("项目层依赖会话：没有会话就没有项目记忆", () => {
  assert.equal(M.projectLayerAvailable(null), false);
  assert.equal(M.projectLayerAvailable(""), false);
  assert.equal(M.projectLayerAvailable("  "), false);
  assert.equal(M.projectLayerAvailable("sid-1"), true);
});

test("两层各有说明文案，且语义不同", () => {
  const p = M.layerDesc("personal"), j = M.layerDesc("project");
  assert.notEqual(p, j);
  assert.match(p, /任何项目|跨项目|你这个人/);
  assert.match(j, /本项目|项目/);
});

test("换层的 owner 取值：个人层为 user，项目层为 null", () => {
  assert.equal(M.ownerForLayer("personal"), "user");
  assert.equal(M.ownerForLayer("project"), null);
});
