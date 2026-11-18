/* frontend/sql_completion.js 的单元测试：node --test frontend/sql_completion.test.mjs */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// 浏览器脚本是 IIFE，会挂到 window.SqlCompletion；测试里用最小 window 跑同一份文件。
const source = readFileSync(new URL("./sql_completion.js", import.meta.url), "utf8");
const sandbox = { window: {} };
vm.runInNewContext(source, sandbox);
const { contextWords, currentWord, mergeWords, tableSuggest, strip, statementBefore, atStatementStart } =
  sandbox.window.SqlCompletion;
const tables = ["products", "v_products", "products_archive", "orders", "customers", "invoices", "payments", "shipments"];
const columns = ["id", "code", "name", "price", "stock", "created_at", "updated_at", "deleted_at"];

test("空编辑器给出命令头，起点是 SELECT", () => {
  const words = contextWords("", columns, tables);
  assert.equal(words[0], "SELECT ");
  for (const expected of ["SELECT * FROM ", "UPDATE ", "WITH ", "INSERT INTO "]) assert.ok(words.includes(expected), expected);
});

test("表名位置上输入前缀时给出表名（含非空目录）", () => {
  // 回归：表名位置的字符类曾写成 [\w.`"]（含 ]），任何 FROM 后的输入都会退化成关键字。
  const words = contextWords("SELECT * FROM ", columns, tables);
  assert.deepEqual([...mergeWords(words, [], "pro")], ["products", "products_archive"]);
  assert.deepEqual([...mergeWords(words, [], "or")], ["orders"]);
});

test("分号之后视为新语句", () => {
  assert.equal(atStatementStart("SELECT 1; "), true);
  assert.equal(atStatementStart("SELECT * FROM products WHERE "), false);
  const words = contextWords("SELECT 1; ", columns, tables);
  assert.ok(words.includes("SELECT "));
  assert.ok(!words.includes('"id"'), "不应沿用上一句的 WHERE 上下文");
});

test("FROM 之后：前缀开头的表名优先，其次才是可执行片段", () => {
  const words = contextWords("SELECT * FROM ", columns, tables);
  assert.deepEqual([...tableSuggest(words, "pro", 10)], ["products", "products_archive"]);
  assert.deepEqual([...tableSuggest(words, "or", 10)], ["orders"]);
  // 没有前缀时不展开整张目录，只给两个起始片段。
  assert.deepEqual([...tableSuggest(words, "", 10)], ["SELECT * FROM ", "SELECT * FROM t LIMIT 20"]);
});

test("WHERE 之后给字段与少量比较符", () => {
  const words = contextWords("SELECT * FROM products WHERE ", columns, tables);
  assert.equal(words[0], '"id"');
  assert.ok(words.length <= 13, `候选过多：${words.length}`);
  assert.ok(words.includes("=") && words.includes("LIKE") && words.includes("IS NULL"));
  assert.deepEqual([...mergeWords(words, [], "na")], ['"name"']);
});

test("AND/OR 之后仍然给字段", () => {
  const words = contextWords("SELECT * FROM products WHERE id = 1 AND ", columns, tables);
  assert.deepEqual([...mergeWords(words, [], "na")], ['"name"']);
});

test("ORDER BY / GROUP BY 之后给字段与方向", () => {
  const words = contextWords("SELECT * FROM products ORDER BY ", columns, tables);
  assert.deepEqual([...mergeWords(words, [], "st")], ['"stock"']);
  assert.ok(words.includes("ASC") && words.includes("DESC"));
});

test("注释和字符串里的 FROM 不切换上下文", () => {
  // 去掉注释/字符串后语句停在 SELECT 1，应得到普通关键字而不是表名。
  assert.ok(contextWords("SELECT 1 -- FROM ", columns, tables).includes("WHERE"));
  assert.ok(!contextWords("SELECT 'FROM ' AS x", columns, tables).includes("products"));
  assert.equal(strip("a /* FROM x */ b -- WHERE c\n d"), "a   b  \n d");
});

test("当前单词按真实光标位置提取", () => {
  assert.equal(currentWord("SELECT * FROM pro"), "pro");
  assert.equal(currentWord("SELECT * FROM "), "");
  assert.equal(currentWord('SELECT "na'), '"na');
  assert.equal(statementBefore("SELECT 1; SELEC"), " SELEC");
});

test("前缀过滤大小写不敏感、去重，且 d 开头能同时给出表与关键字", () => {
  // 传入顺序即优先级：第一个数组先出。
  assert.deepEqual([...mergeWords(['SELECT', 'select * from ', 'UPDATE'], ['SELECTED', 'select'], 'sel')], ['SELECT', 'select * from ', 'SELECTED']);
  assert.deepEqual([...mergeWords(['products', 'SELECT'], ['PRIMARY KEY'], 'pr')], ['products', 'PRIMARY KEY']);
  assert.deepEqual([...mergeWords(['SELECT', 'SET', 'SUM(*)'], ['SHOW TABLES'], 's')], ['SELECT', 'SET', 'SUM(*)', 'SHOW TABLES']);
  assert.deepEqual([...mergeWords(['SELECT'], ['FROM'], '')], []);
  assert.deepEqual([...mergeWords(['LIMIT 20'], ['values'], 'lim')], ['LIMIT 20']);
  assert.deepEqual([...mergeWords([], ['DESC', 'DATE('], 'd')], ['DESC', 'DATE(']);
});
