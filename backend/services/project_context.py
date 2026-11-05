"""Bounded project instructions shared by Web, CLI and read-only subagents."""
import hashlib
import threading
from pathlib import Path

RULE_NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_SOURCE_BYTES = 128 * 1024
MAX_CONTEXT_BYTES = 32 * 1024


class ProjectContext:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve()
        self._directories = {self.root}
        self._lock = threading.Lock()
        self.current = {"sources": [], "diagnostics": [], "content": ""}

    def touch(self, path):
        target = Path(path)
        if not target.is_absolute():
            target = self.root / target
        directory = target.resolve().parent
        if not directory.is_relative_to(self.root):
            return
        with self._lock:
            while directory != self.root:
                if len(self._directories) >= 64:
                    return
                self._directories.add(directory)
                directory = directory.parent

    def refresh(self):
        with self._lock:
            directories = sorted(self._directories, key=lambda p: (len(p.parts), str(p)))
        sources, diagnostics, seen = [], [], set()
        for directory in directories:
            for name in RULE_NAMES:
                path = directory / name
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                        diagnostics.append(f"规则路径越界或为符号链接：{path.relative_to(self.root)}")
                        continue
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_SOURCE_BYTES + 1)
                except FileNotFoundError:
                    continue
                except OSError:
                    diagnostics.append(f"无法读取项目规则：{path.relative_to(self.root)}")
                    continue
                if len(raw) > MAX_SOURCE_BYTES:
                    diagnostics.append(f"规则超过单文件上限：{path.relative_to(self.root)}")
                    continue
                try:
                    text = raw.decode("utf-8-sig").strip()
                except UnicodeError:
                    diagnostics.append(f"规则不是 UTF-8 文本：{path.relative_to(self.root)}")
                    continue
                digest = hashlib.sha256(text.encode()).hexdigest()
                if not text or digest in seen:
                    continue
                seen.add(digest)
                sources.append({"path": str(path.relative_to(self.root)),
                                "sha256": digest, "content": text, "truncated": False})
        # Retain the most specific scopes first, but render broad-to-specific.
        retained, remaining = [], MAX_CONTEXT_BYTES - 2048
        for source in reversed(sources):
            heading = f"\n\n项目规则 {source['path']}\n"
            data = (heading + source["content"]).encode()
            if len(data) > remaining:
                diagnostics.append(f"规则上下文预算不足：{source['path']}")
                # Never silently discard a tail that may contain important constraints.
                continue
            remaining -= len(data)
            retained.append(source)
        retained.reverse()
        body = "".join(f"\n\n项目规则 {s['path']}\n{s['content']}" for s in retained)
        if diagnostics:
            body += "\n\n项目规则诊断\n" + "\n".join(diagnostics).encode()[:1800].decode("utf-8", errors="ignore")
        self.current = {"sources": retained, "diagnostics": diagnostics, "content": body}
        return self.current
