# -*- coding: utf-8 -*-
"""读取 ~/.claude/projects 下的会话 jsonl，给外部会话提供"输出镜像"。"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
TAIL_BYTES = 128 * 1024
PREVIEW_LIMIT = 400
PREVIEW_TITLE_MAX = 28

# 会话列表每 5s 轮询，按 cwd 缓存目录扫描结果，避免反复 glob/stat/读文件头
_file_cache: dict[str, tuple[float, list[dict]]] = {}
_file_cache_lock = threading.Lock()
FILE_CACHE_TTL = 20.0


def encode_cwd(cwd: str) -> str:
    """C:\\Users\\4v -> C--Users-4v（Claude Code 目录编码规则）。"""
    return re.sub(r"[:/\\]", "-", cwd)


def project_dir(cwd: str | None) -> Path | None:
    if not cwd:
        return None
    d = PROJECTS_DIR / encode_cwd(cwd)
    return d if d.is_dir() else None


def _first_timestamp(path: Path) -> datetime | None:
    with path.open("rb") as f:
        head = f.read(8192)
    for ln in head.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        ts = d.get("timestamp")
        if ts:
            return _parse_ts(ts)
    return None


def _ai_title(path: Path) -> str | None:
    with path.open("rb") as f:
        head = f.read(65536)
    for ln in head.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") == "ai-title" and isinstance(d.get("title"), str):
            return d["title"]
    return None


def _preview_title(path: Path) -> str | None:
    """无 ai-title 时的兜底：取首条用户消息的前若干字。"""
    with path.open("rb") as f:
        head = f.read(65536)
    for ln in head.splitlines():
        ev = parse_line(ln)
        if ev and ev.get("role") == "user" and ev.get("kind") == "text":
            text = " ".join(ev["text"].split())
            if text:
                return text[:PREVIEW_TITLE_MAX] + ("…" if len(text) > PREVIEW_TITLE_MAX else "")
    return None


def list_files(cwd: str | None) -> list[dict]:
    d = project_dir(cwd)
    if not d:
        return []
    out = []
    for f in d.glob("*.jsonl"):
        st = f.stat()
        first_ts = _first_timestamp(f)
        out.append({
            "file": f.name,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "size": st.st_size,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "title": _ai_title(f),
            "preview": _preview_title(f),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def list_files_cached(cwd: str | None, ttl: float = FILE_CACHE_TTL) -> list[dict]:
    """list_files 的短 TTL 缓存（会话列表轮询用）。"""
    key = cwd or ""
    now = time.monotonic()
    with _file_cache_lock:
        hit = _file_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    files = list_files(cwd)
    with _file_cache_lock:
        _file_cache[key] = (now, files)
    return files


def session_title(cwd: str | None, started_iso: str | None) -> str | None:
    """该会话的总结性标题：AI 标题优先，否则首条用户消息摘要。"""
    files = list_files_cached(cwd)
    if not files:
        return None
    name = choose_file(cwd, started_iso)
    for f in files:
        if f["file"] == name:
            return f.get("title") or f.get("preview")
    return None


def choose_file(cwd: str | None, started_iso: str | None) -> str | None:
    """为进程选最可能属于它的 jsonl：新会话（首条消息晚于启动）取启动时间最近者，
    否则取最近修改。"""
    files = list_files_cached(cwd)
    if not files:
        return None
    if not started_iso:
        return files[0]["file"]
    started = datetime.fromisoformat(started_iso).astimezone(timezone.utc)
    fresh = []
    for f in files:
        if f["first_ts"]:
            ts = datetime.fromisoformat(f["first_ts"])
            if ts >= started.replace(second=0):
                fresh.append((abs((ts - started).total_seconds()), f["file"]))
    if fresh:
        return sorted(fresh)[0][1]
    return files[0]["file"]


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _tool_detail(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().splitlines()
        return cmd[0][:160] if cmd else "bash"
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return str(inp.get("file_path") or "")
    if name in ("Glob", "Grep"):
        return str(inp.get("pattern") or "")
    if name in ("WebFetch",):
        return str(inp.get("url") or "")
    if name in ("WebSearch",):
        return str(inp.get("query") or "")
    if name in ("Task", "Agent"):
        return str(inp.get("description") or name)
    return name


def parse_line(ln: str) -> dict | None:
    try:
        d = json.loads(ln)
    except ValueError:
        return None
    if d.get("isSidechain"):
        return None
    t = d.get("type")
    ts = d.get("timestamp")
    msg = d.get("message")
    if t == "user" and isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return {"role": "user", "kind": "text", "text": content, "ts": ts}
        if isinstance(content, list):
            parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    raw = b.get("content")
                    txt = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                    tag = "⚠ 工具报错" if b.get("is_error") else "↩ 工具结果"
                    parts.append(f"{tag}: {txt[:PREVIEW_LIMIT]}")
            text = "\n".join(p for p in parts if p).strip()
            return {"role": "user", "kind": "text", "text": text, "ts": ts} if text else None
    if t == "assistant" and isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            texts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    texts.append(b["text"].strip())
                elif bt == "tool_use":
                    return {"role": "assistant", "kind": "tool",
                            "name": b.get("name", "?"),
                            "detail": _tool_detail(b.get("name", ""), b.get("input") or {}),
                            "ts": ts}
            if texts:
                return {"role": "assistant", "kind": "text",
                        "text": "\n".join(texts), "ts": ts}
    return None


def _read_complete(path: Path, start: int, end: int) -> tuple[list[str], int]:
    """读 [start:end)，只返回到最后一个换行符为止的完整行及新的偏移。"""
    if end <= start:
        return [], start
    with path.open("rb") as f:
        f.seek(start)
        chunk = f.read(end - start)
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return [], start
    consumed = last_nl + 1
    text = chunk[:consumed].decode("utf-8", "replace")
    return [ln for ln in text.splitlines() if ln.strip()], start + consumed


def read_events(cwd: str | None, file: str | None, after: int = 0) -> dict:
    d = project_dir(cwd)
    if not d or not file:
        return {"events": [], "size": after, "found": False}
    path = d / file
    if not path.is_file():
        return {"events": [], "size": 0, "found": False}
    size = path.stat().st_size
    # 文件被截断/切换（resume 复用），从头读
    if size < after:
        after = 0
    if after == 0:
        start = max(0, size - TAIL_BYTES)
    else:
        start = after
    lines, new_after = _read_complete(path, start, size)
    events = [e for ln in lines if (e := parse_line(ln))]
    return {"events": events, "size": new_after, "found": True}
