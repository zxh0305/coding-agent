// Minimal asar reader.
const fs = require("fs");
const ASAR = "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar";

let _cache = null;
function header() {
  if (_cache) return _cache;
  const fd = fs.openSync(ASAR, "r");
  const b = Buffer.alloc(16);
  fs.readSync(fd, b, 0, 16, 0);
  const headerSize = b.readUInt32LE(4);
  const jsonLen = b.readUInt32LE(12);
  const jb = Buffer.alloc(jsonLen);
  fs.readSync(fd, jb, 0, jsonLen, 16);
  const j = JSON.parse(jb.toString("utf8"));
  _cache = { fd, headerSize, j, dataOffset: 8 + headerSize };
  return _cache;
}

function nodeAt(p) {
  const { j } = header();
  let node = j;
  const parts = p.split("/").filter(Boolean);
  for (const part of parts) {
    if (!node.files || !node.files[part]) return null;
    node = node.files[part];
  }
  return node;
}

function readFile(p) {
  const { fd, dataOffset } = header();
  const n = nodeAt(p);
  if (!n) throw new Error("not found: " + p);
  if (n.files) throw new Error("is a directory: " + p);
  const buf = Buffer.alloc(n.size);
  fs.readSync(fd, buf, 0, n.size, dataOffset + Number(n.offset));
  return buf.toString("utf8");
}

function listDir(p) {
  const n = nodeAt(p);
  if (!n || !n.files) return [];
  return Object.entries(n.files).map(([k, v]) => ({
    name: k,
    dir: !!v.files,
    size: v.size || 0,
  }));
}

function walk(p, out = []) {
  const n = nodeAt(p);
  if (!n || !n.files) return out;
  for (const [k, v] of Object.entries(n.files)) {
    const cp = p + "/" + k;
    if (v.files) walk(cp, out);
    else out.push({ path: cp, size: v.size });
  }
  return out;
}

module.exports = { readFile, listDir, walk, nodeAt };

if (require.main === module) {
  const [cmd, arg] = process.argv.slice(2);
  if (cmd === "list") console.log(listDir(arg).map(x => (x.dir ? "[D] " : "    ") + x.name + (x.dir ? "" : "  " + x.size)).join("\n"));
  else if (cmd === "read") process.stdout.write(readFile(arg));
  else if (cmd === "walk") console.log(walk(arg).map(x => x.path + "  " + x.size).join("\n"));
}
