"""一次性修复：给没有 frontmatter 的记忆文件补上前缀
================================================================================

用法（默认 dry-run，只打印不落盘）：
    python3 backend/manual/backfill_memory_frontmatter.py            # 预演
    python3 backend/manual/backfill_memory_frontmatter.py --apply    # 真正写入
    python3 backend/manual/backfill_memory_frontmatter.py --apply --dir <记忆目录>

背景（为什么要修）
--------------------------------------------------------------------------------
真实库里 33/74 条记忆是模型当年直接用 write_file 写的，**没有 frontmatter**。
parse_frontmatter 要求 name/description/type 三字段齐全，所以这些文件：

  * 不进 scan_inventory（提取器的"已有记忆清单"）→ 提取器不知道它们存在
    → "更新旧文件而不是新建重复"这条纪律对它们完全失效 → 持续产生重复条目；
  * 在记忆面板上显示成空白描述（Step 3 已标成"未整理"，但标记不等于修好）；
  * 没有 type，无法参与按类型排序/过滤。

修法：从首行 `# 标题` 推出 description，补一个规范的 frontmatter 前缀。
**正文一个字节都不动**——只加头部。原始内容先整目录备份。

type 一律填 project：这 33 条按位置都在项目层，且内容都是该项目的实现/修复
记录（属于"这个代码库的事实"）。若某条实际该归个人层，用户在面板里一键
"移到个人层"即可——那正是 Step 3 提供的能力，比脚本猜得准。

幂等：已有 frontmatter 的文件一律跳过，重复执行无副作用。
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import memory  # noqa: E402

DESC_MAX = 80          # 索引行里的钩子不能太长（见 memory.render_index_line）


def _title_and_desc(text: str) -> tuple[str, str]:
    """从文件正文推出 (标题, 描述)。

    首行是 `# 标题` 就直接用它；否则退到第一个非空行。标题过长时截断——
    索引行是模型召回时唯一的判断依据，一整段话塞进去反而降低信噪比。
    """
    lines = [ln for ln in (text or "").splitlines()]
    title = ""
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        title = s.lstrip("#").strip() if s.startswith("#") else s
        break
    title = " ".join(title.split())
    if not title:
        return "", "(无标题)"
    if len(title) > DESC_MAX:
        title = title[:DESC_MAX].rstrip() + "…"
    return title, title


def backfill_one(path: Path) -> str | None:
    """给单个文件补 frontmatter。返回 description（成功）或 None（跳过）。

    只加头部，正文原样：用 render_memory_file 重组会规范化正文空白，
    对这些手工写下的记录是破坏——所以这里手工拼前缀。
    """
    text = path.read_text(encoding="utf-8")
    if memory.parse_frontmatter(text)[0] is not None:
        return None                      # 已有 frontmatter：幂等跳过
    title, desc = _title_and_desc(text)
    if not title:
        title = path.stem
    fm = memory.render_frontmatter(path.stem, desc, "project")
    memory.atomic_write_text(path, fm + text)
    return desc


def backfill_dir(mem_dir: Path, apply: bool) -> dict:
    """扫一个记忆目录并补全。返回统计。"""
    stats = {"scanned": 0, "fixed": 0, "skipped": 0, "names": []}
    if not mem_dir.is_dir():
        return stats
    for p in sorted(mem_dir.glob("*.md")):
        if p.name == memory.INDEX_NAME:
            continue
        stats["scanned"] += 1
        if not apply:
            # 预演：只判断"是否需要补"，不写盘
            if memory.parse_frontmatter(p.read_text(encoding="utf-8"))[0] is None:
                _, desc = _title_and_desc(p.read_text(encoding="utf-8"))
                stats["fixed"] += 1
                stats["names"].append((p.name, desc))
            else:
                stats["skipped"] += 1
            continue
        desc = backfill_one(p)
        if desc is None:
            stats["skipped"] += 1
        else:
            stats["fixed"] += 1
            stats["names"].append((p.name, desc))
    return stats


def rebuild_index(mem_dir: Path) -> int:
    """按补全后的文件重建索引行（补了 frontmatter 才谈得上"索引里有钩子"）。

    只处理**索引里没有**的条目（append-only）：已有索引行是提取器/用户写的，
    整表重建会丢掉人工措辞。缺的那些按 description 补一行。
    """
    index = memory.read_index_text(mem_dir)
    added = 0
    for p in sorted(mem_dir.glob("*.md")):
        if p.name == memory.INDEX_NAME:
            continue
        link = f"]({p.name})"
        if link in index:
            continue
        meta, _ = memory.parse_frontmatter(p.read_text(encoding="utf-8"))
        desc = (meta or {}).get("description") or p.stem
        index = memory.upsert_index_line(index, p.name, desc)
        added += 1
    if added:
        memory.atomic_write_text(mem_dir / memory.INDEX_NAME, index)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省只预演）")
    ap.add_argument("--dir", default=None, help="记忆目录（缺省遍历所有项目层）")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不建议）")
    args = ap.parse_args()

    root = memory._memory_root()
    if args.dir:
        targets = [Path(args.dir)]
    else:
        targets = sorted(p for p in (root / memory.PROJECTS_SUBDIR).glob("*") if p.is_dir())
        targets += sorted(p for p in (root / memory.USERS_SUBDIR).glob("*") if p.is_dir())
    if not targets:
        print(f"没有找到记忆目录（{root}）", file=sys.stderr)
        return 1

    if args.apply and not args.no_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = root.parent / "backups" / f"memory-pre-backfill-{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, backup / "memory", dirs_exist_ok=True)
        print(f"备份 → {backup}")

    total_fixed = 0
    for d in targets:
        stats = backfill_dir(d, args.apply)
        if not stats["scanned"]:
            continue
        print(f"\n{d.name}：{stats['scanned']} 条扫描，需补 {stats['fixed']}，已有跳过 {stats['skipped']}")
        for name, desc in stats["names"]:
            print(f"    + {name}  ←  {desc[:60]}")
        total_fixed += stats["fixed"]
        if args.apply and stats["fixed"]:
            n = rebuild_index(d)
            if n:
                print(f"    索引补 {n} 行")

    print(f"\n{'已补' if args.apply else '待补'} {total_fixed} 条"
          f"{'' if args.apply else '（预演模式，加 --apply 才写入）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
