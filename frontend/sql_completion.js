/* SQL 控制台补全的纯数据与纯函数；弹层交互见 sql_completion_ui.js。
   返回的片段都是完整可替换的词：插入时直接替换光标前的单词，不产生重复拼写。 */
(() => {
  // 与 FROM 位置规则保持一致：[/\w."`]* 不含 ]（否则 FROM x] 会退化为关键字）与反斜杠。
  // 命令头只在语句开头或分号后给出，避免在语句中间插入 UPDATE 之类的片段。
  const HEADS = [
    'SELECT ', 'SELECT * FROM ', 'SELECT DISTINCT ', 'SELECT COUNT(*) FROM ',
    'WITH ', 'INSERT INTO ', 'UPDATE ', 'DELETE FROM ', 'CREATE TABLE ', 'ALTER TABLE ', 'DROP TABLE ', 'EXPLAIN ',
  ];
  // 表名后可直接执行的首行；tableName 在连接时可替换为实际表名。
  const TABLE_FRAGMENTS = ['SELECT * FROM ', 'SELECT * FROM t LIMIT 20', 'SELECT COUNT(*) FROM t'];
  const WHERE_OPERATORS = ['=', '<>', '>=', '<=', 'LIKE', 'NOT LIKE', 'IN (', 'BETWEEN ', 'IS NULL', 'IS NOT NULL', 'AND ', 'OR '];
  const KEYWORDS = [
    'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'IS', 'LIKE', 'BETWEEN', 'EXISTS',
    'ORDER BY', 'GROUP BY', 'HAVING', 'LIMIT', 'OFFSET', 'DISTINCT', 'AS', 'ON',
    'JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'INNER JOIN', 'UNION', 'UNION ALL', 'ASC', 'DESC', 'NULL',
    'COUNT(*)', 'SUM(*)', 'AVG(*)', 'MIN(*)', 'MAX(*)', 'COALESCE(', 'IFNULL(', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
    'INSERT INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE FROM', 'CREATE TABLE', 'ALTER TABLE', 'DROP TABLE',
    'ADD COLUMN', 'PRIMARY KEY', 'DEFAULT', 'CURRENT_TIMESTAMP', 'WITH', 'EXPLAIN', 'SHOW TABLES', 'PRAGMA',
  ];
  const TABLE_SHORTCUTS = ['SELECT * FROM t WHERE ', 'SELECT * FROM t ORDER BY name DESC LIMIT 20', 'UPDATE t SET name = ', 'DELETE FROM t WHERE '];

  const lower = (text) => String(text).toLowerCase();
  const normalize = (text) => lower(text).replace(/\s+/g, ' ').trim();
  const quote = (name) => '"' + String(name).replace(/"/g, '""') + '"';
  // 去掉注释与字符串字面量：其中的 FROM/WHERE 不应改变补全上下文。
  function strip(text) {
    return String(text)
      .replace(/\/\*[\s\S]*?\*\//g, ' ')
      .replace(/--[^\n]*/g, ' ')
      .replace(/'(?:[^']|'')*'/g, "''");
  }
  // 光标所在语句（最后一个分号之后）里光标前的文本。
  function statementBefore(text) {
    const clean = strip(text);
    const cut = clean.lastIndexOf(';');
    return cut < 0 ? clean : clean.slice(cut + 1);
  }
  // 每个上下文给出「前缀 + 少量足量候选」：没有输入前缀时绝不把整张表铺满屏幕。
  const CONTEXTS = [
    // 表名：FROM/JOIN/UPDATE/INTO 之后，只给命令片段与少量表名（双引号是开头的标识符引号）。
    { test: /(?:^|\s)(?:from|join|into|update|table)\s+[\w."`]*$/i,
      words: (columns, tables) => [...TABLE_FRAGMENTS.slice(0, 2), ...(tables || [])] },
    { test: /(?:^|\s)(?:where|having|set)\s+.*$/i,
      words: (columns) => [...columnWords(columns).slice(0, 8), '=', 'LIKE', 'IS NULL', 'AND ', 'OR '] },
    { test: /(?:^|\s)(?:and|or)\s+[\w."`]*$/i,
      words: (columns) => [...columnWords(columns).slice(0, 8), 'LIKE', 'IS NULL', 'AND ', 'OR '] },
    { test: /(?:order|group)\s+by\s+.*$/i,
      words: (columns) => [...columnWords(columns).slice(0, 8), 'ASC', 'DESC'] },
  ];
  function columnWords(columns) {
    return (columns || []).map(quote);
  }
  function contextWords(text, columns, tables) {
    const statement = statementBefore(text);
    // 空编辑器或分号后视为新语句：给命令头，起点词优先。
    if (!statement.trim()) return wordsUnique(HEADS);
    for (const context of CONTEXTS) if (context.test.test(statement)) return context.words(columns, tables);
    // 其他位置：通用关键字；光标前是空白时再附几条表名，输入 d 也能先看到 desc 之类的表。
    return [...KEYWORDS, ...(atWordStart(text) ? (tables || []).slice(0, 6) : []), ...(tables && tables.length ? [] : TABLE_SHORTCUTS)];
  }
  // 当前光标前的半截词（用于前缀过滤与替换范围）；光标紧贴标识符时才触发。
  function currentWord(text) {
    const match = /[\w`".]+$/.exec(text);
    return match ? match[0] : '';
  }
  // 光标前是空、空白或标点：此时适合直接列出候选，而不是要求先输入前缀。
  function atWordStart(text) { return /(?:^|[\s(,=.])$/.test(text); }
  // 命令头位置（语句开头或分号后）：只在尚未输入前缀时给出 SELECT 等起点。
  function atStatementStart(text) { return !statementBefore(text).trim(); }
  // 引号是标识符的一部分，但不是用户输入的前缀：输入 na 应匹配 "name"。
  const bare = (text) => String(text).replace(/^["`\[]+/, '');
  function mergeWords(staticWords, typedWords, prefix, limit) {
    const query = normalize(bare(prefix));
    if (!query) return [];
    const seen = new Set();
    const out = [];
    const add = (word) => {
      const key = normalize(bare(word));
      // 表名不做加引号的标识符包装：已在引号内输入时（FROM "pro）也要能匹配。
      if (!key.startsWith(query) || seen.has(key)) return;
      seen.add(key); out.push(word);
    };
    // 调用方决定顺序：先给的数组优先（语句开头与 WHERE 等位置关键字在前，表名位置表在前）。
    for (const word of staticWords || []) add(word);
    for (const word of typedWords || []) add(word);
    return out.slice(0, limit || 12);
  }
  // 与 mergeWords 同一套匹配规则，但按「交错」取词：先两边各取第 1 个，再各取第 2 个……
  // 结果不依赖插入顺序：表名位置先表名，其它位置关键字与表名交替出现（输入 d: DESC、desc_tb）。
  // 每组最多看到两组数组的长度合计，因此大目录也只取前面少量表名。
  function suggest(primary, secondary, prefix, limit, primaryFirst) {
    const groups = primaryFirst === 2 ? [secondary || [], primary || []] : [primary || [], secondary || []];
    const query = normalize(bare(prefix));
    if (!query) return [];
    const seen = new Set();
    const out = [];
    const take = (group, index) => {
      const word = group[index];
      if (word === undefined) return;
      const key = normalize(bare(word));
      if (key.startsWith(query) && !seen.has(key)) { seen.add(key); out.push(word); }
    };
    for (let index = 0; out.length < (limit || 10); index += 1) {
      if (index >= Math.max(groups[0].length, groups[1].length)) break;
      for (const group of groups) take(group, index);
    }
    return out.slice(0, limit || 10);
  }
  // 表名位置专用：前缀开头的表名（按目录顺序）优先，片段其次；没有前缀时不展开整张目录。
  function tableSuggest(words, prefix, limit) {
    const query = normalize(bare(prefix));
    const all = words || [];
    const names = all.filter((word) => !TABLE_FRAGMENTS.includes(word));
    const fragments = all.filter((word) => TABLE_FRAGMENTS.includes(word));
    if (!query) return fragments.slice(0, limit || 10);
    return mergeWords(names, fragments, query, limit || 10);
  }
  function wordsUnique(list) {
    const seen = new Set();
    return (list || []).filter((word) => {
      const key = normalize(word);
      if (!key || seen.has(key)) return false;
      seen.add(key); return true;
    });
  }
  window.SqlCompletion = { contextWords, currentWord, atWordStart, atStatementStart, mergeWords, suggest, tableSuggest, strip, statementBefore, quote, wordsUnique, KEYWORDS, TABLE_SHORTCUTS, HEADS };
})();
