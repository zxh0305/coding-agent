#!/usr/bin/env python3
"""
Prompt Cache 透传探测脚本
=========================================================
用途：判断当前供应商（含本地中转）是否真的透传了 prompt caching。
背景：本地 llm_client 已按 anthropic 规范打了 cache_control 断点，但若中转站
不透传，响应里的 cache_read_input_tokens 恒为 0——表现为"每轮全量重算上下文"，
是长会话慢的主因之一（见 .agent-memory/llm-latency-diagnosis.md）。

原理：连发两次【前缀完全相同】的请求，第二次若命中缓存，其
cache_read_input_tokens 应显著 > 0。两次都 0 = 未透传。

用法：
    python3 tools/probe_prompt_cache.py            # 用 .env 里的默认供应商
    python3 tools/probe_prompt_cache.py --tries 3  # 多发几次（首次写缓存有延迟）

输出：每次请求的 input / cache_read / cache_creation tokens，末尾给结论。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

# —— 读 .env 里的默认供应商（不依赖数据库，脚本可独立跑）——
def load_env():
    env = {}
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# 一段足够长的稳定前缀（≥ 1024 token 才有缓存意义，这里约 3k+ 字符）
PREFIX = ("你是一个严谨的工程助手。以下是一段用于测试 prompt cache 的稳定前缀，" * 60)


def load_providers():
    """从数据库读供应商列表（脚本实际用的就是库里配置的中转，不是 .env）。

    返回 [{id, name, base_url, api_format, api_key}]；读不到返回 []。
    """
    import sqlite3
    db_path = Path(__file__).resolve().parent.parent / "data" / "agent_data.db"
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(providers)")}
        keycol = "api_key" if "api_key" in cols else ("key" if "key" in cols else None)
        rows = con.execute(
            f"SELECT id, name, base_url, api_format{',' + keycol if keycol else ''} "
            "FROM providers"
        ).fetchall()
        return [{
            "id": r["id"], "name": r["name"], "base_url": r["base_url"],
            "api_format": r["api_format"],
            "api_key": r[keycol] if keycol else "",
        } for r in rows]
    except Exception:
        return []


def build_body(model, use_cache):
    """构造一次请求体：system 前缀带 cache_control 断点（对齐 llm_client 的做法）。"""
    system = [{"type": "text", "text": PREFIX}]
    if use_cache:
        system[0]["cache_control"] = {"type": "ephemeral"}
    return {
        "model": model,
        "max_tokens": 16,
        "system": system,
        "messages": [{"role": "user", "content": "只回复一个词：好"}],
    }


def main():
    ap = argparse.ArgumentParser(description="探测供应商是否透传 prompt cache")
    ap.add_argument("--tries", type=int, default=2, help="总请求次数（默认 2）")
    ap.add_argument("--no-cache", action="store_true", help="对照组：不打 cache_control")
    ap.add_argument("--provider", help="供应商 id 或名称子串（从数据库读；不传则用 .env）")
    ap.add_argument("--model", help="模型名（覆盖 .env/默认；查特定模型是否透传缓存时用）")
    ap.add_argument("--list", action="store_true", help="列出数据库里可选的供应商后退出")
    args = ap.parse_args()

    providers = load_providers()
    if args.list:
        if not providers:
            print("（读不到数据库供应商；请用 .env 的 LLM_BASE_URL 方式）")
        for p in providers:
            print(f"  {p['id']:<8} {p['name']:<16} [{p['api_format']}] {p['base_url']}")
        return 0

    if args.provider:
        # 按 id 或名称子串匹配库里的供应商
        hit = [p for p in providers
               if p["id"] == args.provider or args.provider in p["name"]]
        if not hit:
            print(f"✗ 库里没找到供应商「{args.provider}」；用 --list 看可选列表")
            return 2
        p = hit[0]
        base, key, fmt = p["base_url"], p["api_key"], p["api_format"]
        # 模型名：库里没有就退回 .env 的 LLM_MODEL
        env0 = load_env()
        model = args.model or env0.get("LLM_MODEL") or "deepseek-v4-pro"
        print(f"（供应商来自数据库：{p['name']}，模型 {model}）")
        env = {"LLM_API_FORMAT": fmt}
    else:
        env = load_env()
        base = env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL")
        key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
        model = args.model or env.get("LLM_MODEL") or env.get("OPENAI_MODEL")
    if not (base and key and model):
        print("✗ 缺 base/key/model：用 --provider <id|名称> 指定库里的供应商，")
        print("  或在 .env 里补 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
        return 2

    import urllib.request
    # 自适应格式：.env 里若显式给 api_format 用它；否则按 base 猜（/v1 或 deepseek
    # 走 OpenAI 兼容的 /chat/completions，含 anthropic 字样走 /messages）
    fmt = env.get("LLM_API_FORMAT", "")
    if not fmt:
        fmt = "anthropic" if "anthropic" in base.lower() else "openai"
    if fmt == "anthropic":
        url = base.rstrip("/") + "/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = build_body(model, use_cache=not args.no_cache)
    else:
        url = base.rstrip("/") + ("/chat/completions" if not base.rstrip("/").endswith("/chat/completions") else "")
        headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
        # OpenAI 格式的缓存：在消息 content 上打 cache_control（随供应商支持而异）
        body = {"model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": PREFIX + "\n只回复一个词：好"}]}
        if not args.no_cache:
            body["messages"][0]["content"] = [
                {"type": "text", "text": PREFIX, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "只回复一个词：好"}]

    print(f"供应商: {base}\n模型: {model}\n格式: {fmt}\ncache_control: {'关' if args.no_cache else '开'}\n")
    rows = []
    for i in range(args.tries):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"第 {i+1} 次 HTTP {e.code}: {e.read()[:200]}")
            return 1
        except Exception as e:
            print(f"第 {i+1} 次请求失败: {e}")
            return 1
        dt = time.time() - t0
        u = (payload or {}).get("usage", {})
        # 两种格式的字段名（anthropic: input_tokens/cache_read_input_tokens；
        # openai/deepseek: prompt_tokens/prompt_cache_hit_tokens）
        row = {
            "input": u.get("input_tokens", u.get("prompt_tokens", 0)),
            "cache_read": u.get("cache_read_input_tokens", u.get("prompt_cache_hit_tokens", 0)),
            "cache_write": u.get("cache_creation_input_tokens", 0),
            "output": u.get("output_tokens", u.get("completion_tokens", 0)),
            "secs": round(dt, 2),
        }
        rows.append(row)
        print(f"第 {i+1} 次: input={row['input']:<6} cache_read={row['cache_read']:<6} "
              f"cache_write={row['cache_write']:<6} {row['secs']}s")

    reads = [r["cache_read"] for r in rows[1:]]
    print("\n结论:", end=" ")
    if reads and any(x > 0 for x in reads):
        print(f"✓ 缓存已透传（第 2 次起 cache_read > 0，命中 {max(reads)} tokens）")
    elif rows and rows[0]["cache_write"] > 0:
        print("△ 写入了缓存但没读到——可能首次尚未生效，把 --tries 调大再试")
    else:
        print("✗ 未透传（cache_read 与 cache_write 均为 0）——每轮都在全量重算上下文。\n"
              "  建议：找中转站确认是否转发了 anthropic 的 prompt caching 字段。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
