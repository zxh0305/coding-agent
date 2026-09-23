"""
归档文件处理（Archive handling）
================================

运维日志按天打包成 `.tar.gz` / `.zip` 是绝对主流——用户上传的附件里，
压缩包占比很高。但附件读取链路（read_attachment）是按 UTF-8 文本解码的，
压缩包是二进制流，解码必然得到一屏乱码，模型拿不到任何有用信息。

本模块解决两件事：

  1. **识别**：判断一个文件是不是压缩包（按扩展名 + 魔数双重判断，
     不信任扩展名——`.log` 也可能是个 gzip 流）。
  2. **安全解压**：把归档解开到一个受控目录，逐成员校验落点，
     并施加"输出总量上限 + 成员数上限"，防 gzip 炸弹与 Zip Slip。

设计原则（与项目其它模块一致）：
  * 纯标准库（tarfile / zipfile / gzip / lzma / bz2），零第三方依赖；
  * 失败不抛到调用方崩掉——返回结构化结果，让上层兜成工具错误信封；
  * 一切路径写入前都 resolve() 校验，绝不允许逃出目标目录。

为什么不直接把附件落进工作区：上传发生在会话绑定工作区之前（见 app.py），
且附件区必须按会话隔离、跟随库文件走。本模块改为"按需解压到会话附件区内的
_extracted/ 子目录"，再让附件区对 agent 可达（见 code_tools 的附件桥接）。
"""

import gzip
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

# 解压后允许的总字节上限（防 gzip 炸弹：1MB 的 gzip 能炸出 10GB）。
MAX_EXTRACT_BYTES = 200 * 1024 * 1024
# 单个归档内允许的成员数上限（防一个 tar 里塞几万个文件）。
MAX_EXTRACT_MEMBERS = 50
# 单次解压的墙钟预算（秒），防止畸形归档拖死请求线程。
MAX_EXTRACT_SECONDS = 30

# 归档类型：扩展名 -> 展示用名称。
_ARCHIVE_SUFFIXES = (
    (".tar.gz", "tar.gz"), (".tgz", "tar.gz"),
    (".tar.bz2", "tar.bz2"), (".tbz2", "tar.bz2"),
    (".tar.xz", "tar.xz"), (".txz", "tar.xz"),
    (".tar", "tar"),
    (".zip", "zip"),
    (".gz", "gzip"), (".bz2", "bzip2"), (".xz", "xz"),
)

# 魔数：即使扩展名被改坏也能认出（gzip 1f 8b；zip PK\x03\x04）。
_MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
)


def detect_archive(path: Path) -> str | None:
    """判断文件是不是压缩包，是则返回类型名（"tar.gz"/"zip"/...），否则 None。

    先看扩展名（快、覆盖绝大多数），再用魔数兜底——用户把 `.log` 直接
    gzip 了但没改名的场景很常见。
    """
    name = path.name.lower()
    for suffix, kind in _ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            return kind
    # 扩展名不认识：读头 8 字节看魔数（不整文件读，避免大文件开销）
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        return None
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            # gzip 魔数也可能是 .tar.gz 被改名，但类型展示上先归 gzip
            return kind
    return None


def is_probably_binary(path: Path, probe: int = 8192) -> bool:
    """粗判文件是不是二进制（含 NUL 字节即认为是）。

    只读头部 probe 字节：真正要判断的是"能否当文本给模型读"，
    头部有 NUL 基本可以断定不是文本，没必要扫全文。
    """
    try:
        with path.open("rb") as f:
            chunk = f.read(probe)
    except OSError:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    # 解码失败率高也视为二进制
    try:
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _safe_join(base: Path, member_name: str) -> Path | None:
    """把归档内的成员名解析到 base 之内；越界（Zip Slip / ../）返回 None。"""
    # 归一化：去掉前导 / 与盘符，消解 ../
    cleaned = member_name.replace("\\", "/").lstrip("/")
    base_r = base.resolve()
    target = (base_r / cleaned).resolve()
    if target == base_r or base_r not in target.parents:
        return None
    return target


def _check_budget(total: int, count: int) -> str | None:
    """预算校验：超限返回原因字符串，未超返回 None。"""
    if count > MAX_EXTRACT_MEMBERS:
        return f"归档成员数超过上限（{MAX_EXTRACT_MEMBERS} 个）"
    if total > MAX_EXTRACT_BYTES:
        return f"解压后总量超过上限（{MAX_EXTRACT_BYTES} 字节），疑似压缩炸弹"
    return None


def _extract_tar(path: Path, dest: Path) -> dict:
    """解压 tar 系列（含 .tar.gz/.tar.bz2/.tar.xz 与裸 .tar）。"""
    members_info, total, count = [], 0, 0
    with tarfile.open(path, "r:*") as tf:
        for member in tf:
            count += 1
            # 只接受普通文件与目录：符号链接/设备文件一律跳过（防逃逸）
            if member.issym() or member.islnk():
                members_info.append({"name": member.name, "skipped": "链接（安全起见不解压）"})
                continue
            if not (member.isfile() or member.isdir()):
                members_info.append({"name": member.name, "skipped": "特殊文件类型"})
                continue
            total += max(0, member.size)
            reason = _check_budget(total, count)
            if reason:
                raise ValueError(reason)
            target = _safe_join(dest, member.name)
            if target is None:
                members_info.append({"name": member.name, "skipped": "路径越界（Zip Slip）"})
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=64 * 1024)
            members_info.append({"name": member.name, "bytes": member.size})
    return {"members": members_info, "total_bytes": total}


def _extract_zip(path: Path, dest: Path) -> dict:
    """解压 zip（逐成员校验，防 Zip Slip）。"""
    members_info, total, count = [], 0, 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            count += 1
            if info.is_dir():
                target = _safe_join(dest, info.filename)
                if target is not None:
                    target.mkdir(parents=True, exist_ok=True)
                continue
            total += max(0, info.file_size)
            reason = _check_budget(total, count)
            if reason:
                raise ValueError(reason)
            target = _safe_join(dest, info.filename)
            if target is None:
                members_info.append({"name": info.filename, "skipped": "路径越界（Zip Slip）"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=64 * 1024)
            members_info.append({"name": info.filename, "bytes": info.file_size})
    return {"members": members_info, "total_bytes": total}


def _extract_single_stream(path: Path, dest: Path, kind: str) -> dict:
    """解压单文件流（.gz/.bz2/.xz）：成员名 = 去掉压缩后缀的原名。"""
    name = path.name
    for suffix in (".gz", ".bz2", ".xz"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        name = name + ".out"
    target = _safe_join(dest, name) or (dest / "extracted.out")

    if kind == "gzip":
        opener = lambda: gzip.open(path, "rb")
    elif kind == "bzip2":
        import bz2
        opener = lambda: bz2.open(path, "rb")
    else:
        import lzma
        opener = lambda: lzma.open(path, "rb")

    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with opener() as src, target.open("wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EXTRACT_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise ValueError(f"解压后总量超过上限（{MAX_EXTRACT_BYTES} 字节），疑似压缩炸弹")
            out.write(chunk)
    return {"members": [{"name": name, "bytes": total}], "total_bytes": total}


def extract_archive(path: Path, dest: Path) -> dict:
    """把归档解压到 dest 目录，返回 {ok, kind, members, total_bytes, extracted_dir}。

    失败返回 {ok: False, error}，不抛异常——调用方（工具层）负责兜成信封。
    dest 会在解压前清空重建，保证幂等（重复解压不叠加）。
    """
    kind = detect_archive(path)
    if kind is None:
        return {"ok": False, "error": "不是可识别的压缩包"}
    # 清空重建：同一附件重复解压时以最后一次为准
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if kind in ("tar.gz", "tar.bz2", "tar.xz", "tar"):
            result = _extract_tar(path, dest)
        elif kind == "zip":
            result = _extract_zip(path, dest)
        else:  # gzip / bzip2 / xz 单文件流
            result = _extract_single_stream(path, dest, kind)
    except ValueError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": str(e)}
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as e:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": f"解压失败：{type(e).__name__}: {e}"}

    real = [m for m in result["members"] if "skipped" not in m]
    return {
        "ok": True,
        "kind": kind,
        "members": result["members"],
        "file_count": len(real),
        "total_bytes": result["total_bytes"],
        "extracted_dir": dest,
    }


def describe_archive(path: Path) -> dict:
    """只列出归档成员清单，不解压（stat 语义，给模型"先看结构"用）。

    返回 {kind, members:[{name,bytes}], file_count, total_bytes}。
    只读元数据，不落盘，开销极小。
    """
    kind = detect_archive(path)
    if kind is None:
        return {"kind": None, "members": [], "file_count": 0, "total_bytes": 0}
    members, total = [], 0
    try:
        if kind in ("tar.gz", "tar.bz2", "tar.xz", "tar"):
            with tarfile.open(path, "r:*") as tf:
                for m in tf:
                    if m.isfile():
                        members.append({"name": m.name, "bytes": m.size})
                        total += max(0, m.size)
                    elif m.isdir():
                        members.append({"name": m.name + "/", "bytes": 0})
        elif kind == "zip":
            with zipfile.ZipFile(path) as zf:
                for i in zf.infolist():
                    members.append({"name": i.filename, "bytes": i.file_size})
                    total += max(0, i.file_size)
        else:
            # 单文件流：只知压缩后大小，解压后大小未知
            members.append({"name": path.name, "bytes": path.stat().st_size,
                            "note": "单文件压缩流，解压后大小未知"})
            total = path.stat().st_size
    except Exception as e:  # 元数据都读不出来：不是有效归档
        return {"kind": None, "members": [], "file_count": 0, "total_bytes": 0,
                "error": f"归档元数据读取失败：{e}"}
    return {"kind": kind, "members": members[:MAX_EXTRACT_MEMBERS],
            "file_count": len(members), "total_bytes": total}
