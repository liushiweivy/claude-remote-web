# -*- coding: utf-8 -*-
"""Claude Code 官方会话登记：~/.claude/sessions/<pid>.json（2.1.283+）。

CC 为每个交互会话维护一个以 **pid 命名**的登记文件，内容含当前 sessionId、
cwd、status，/clear 或续写开新会话时**原地改写 sessionId**。这是时间启发式
无法错配的 ground truth：同 cwd 四开窗口 + 跨小时 /clear 的场景下，纯时间匹配
会把四个会话整体错位一轮（2026-10-21 三代串台实测：21308 的 de1c7c2f 被
23924 偷走、23924 的 fb195c6d 被挂给 24580……每窗看到的都是隔壁记录）。

本模块只读登记、按 (mtime_ns, size) 缓存解析结果；文件缺失/旧版 CC 无登记时
调用方回退到 transcripts.resolve_files 的时间匹配，绝不因读不到登记而返回错文件。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import transcripts, winproc

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
SCAN_TTL_SEC = 2.0
_SID_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")

# Windows FILETIME（100ns tick，1601 纪元）与 Unix 纪元之差
FILETIME_EPOCH_TICKS = 116444736000000000
# procStart 实测与 winproc.process_started 精确到微秒一致，2s 容差仅防读取竞态
PROCSTART_TOL_SEC = 2.0
# startedAt 是 CC 会话初始化时刻，实测比 OS 进程出生晚 2.3~3.6s
STARTEDAT_TOL_SEC = 10.0

_lock = threading.Lock()
_scan_cache: dict = {"ts": 0.0, "items": {}}
# 单文件签名缓存：{pid: ((mtime_ns, size), parsed)}，登记被原地改写时才重读
_file_cache: dict[int, tuple[tuple, dict]] = {}


def _read_pid_file(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    pid_part = path.stem.split(".", 1)[0]
    try:
        pid = int(pid_part)
    except ValueError:
        return None
    with _lock:
        cached = _file_cache.get(pid)
        if cached and cached[0] == sig:
            return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
        return None
    if not _SID_RE.match(data["sessionId"]) or data.get("pid") != pid:
        return None
    with _lock:
        _file_cache[pid] = (sig, data)
    return data


def entries(ttl: float = SCAN_TTL_SEC) -> dict[int, dict]:
    """当前全部登记 {pid: 登记 dict}；目录扫描带 2s TTL，单文件按签名缓存。"""
    now = time.monotonic()
    with _lock:
        hit = _scan_cache["items"]
        if ttl > 0 and hit and now - _scan_cache["ts"] < ttl:
            return dict(hit)
    items: dict[int, dict] = {}
    alive_files: set[int] = set()
    try:
        paths = list(SESSIONS_DIR.glob("*.json"))
    except OSError:
        paths = []
    for p in paths:
        data = _read_pid_file(p)
        if data is not None:
            items[data["pid"]] = data
            alive_files.add(data["pid"])
    with _lock:
        for dead in set(_file_cache) - alive_files:
            del _file_cache[dead]
        _scan_cache["ts"] = now
        _scan_cache["items"] = dict(items)
    return items


def for_pid(pid: int, ttl: float = SCAN_TTL_SEC) -> dict | None:
    """直接读指定 pid 的登记（与 entries 共享目录 TTL 与单文件签名缓存）。"""
    return entries(ttl).get(pid)


def _norm_cwd(cwd: str | None) -> str:
    return os.path.normcase(os.path.normpath(cwd)) if cwd else ""


def _entry_birth(entry: dict) -> tuple[datetime | None, bool]:
    """登记对应的 OS 进程出生时刻（UTC）及其来源。

    优先 procStart（Windows FILETIME 字符串，实测与 winproc 精确到微秒一致，
    可直接证伪 pid 复用残留）；缺失/坏值时退回 startedAt（epoch ms，CC 初始化
    时刻，比出生晚数秒）。返回 (时刻, 是否来自 procStart)；都不可解析 (None,False)。
    """
    raw = entry.get("procStart")
    if isinstance(raw, str) and raw.isdigit():
        try:
            epoch_sec = (int(raw) - FILETIME_EPOCH_TICKS) / 1e7
            return datetime.fromtimestamp(epoch_sec, tz=timezone.utc), True
        except (OverflowError, OSError, ValueError):
            pass
    ms = entry.get("startedAt")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool):
        try:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc), False
        except (OverflowError, OSError, ValueError):
            pass
    return None, False


def entry_is_live(entry: dict, cwd: str | None, started_iso: str | None) -> bool:
    """登记是否属于**当前这个**活进程，而不是 taskkill 后被复用 pid 的陈旧残留。

    必须同时满足：
    1. cwd 一致（会话台所有窗口同 cwd，单靠这条不够，但可拦跨目录复用）；
    2. 有现进程出生时间可比对时，登记的 procStart 精确匹配（容差 2s），
       无 procStart 则 startedAt 在容差 10s 内（实测晚 2.3~3.6s）；
       登记时间字段全坏时 fail-closed 不覆盖；
    3. 现进程出生时间读不到（started_iso 为 None）时退化为只验 cwd。
    """
    if _norm_cwd(entry.get("cwd")) != _norm_cwd(cwd):
        return False
    if not started_iso:
        return True
    try:
        actual = datetime.fromisoformat(started_iso)
    except ValueError:
        return False
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    else:
        actual = actual.astimezone(timezone.utc)
    birth, from_procstart = _entry_birth(entry)
    if birth is None:
        return False
    tol = PROCSTART_TOL_SEC if from_procstart else STARTEDAT_TOL_SEC
    return abs((actual - birth).total_seconds()) <= tol


def for_roots(roots: dict[int, str | None],
              ttl: float = SCAN_TTL_SEC) -> dict[int, dict]:
    """托管 PTY：登记 pid 是 claude.exe，托管 id 对应 root（cmd.exe），
    沿父链把登记 pid 归到其 root。

    roots: {root_pid: root 的 cwd}；只接受 cwd 与 root 一致的登记
    （root 下 Bash 起的别的项目 claude 不得冒领）。同一 root 多条时
    取**父链最浅**者（托管本体直接 spawn 的 claude），同深度再取最近更新。
    返回 {root_pid: 登记 dict}（找不到的 root 不出现在结果里）。
    """
    if not roots:
        return {}
    rows = winproc.snapshot()
    parents = {pid: ppid for pid, ppid, _name in rows}
    want_cwd = {root: _norm_cwd(cwd) for root, cwd in roots.items()}
    all_entries = entries(ttl)
    # {root: [(depth, updatedAt, entry), ...]}
    out: dict[int, list[tuple[int, float, dict]]] = {}
    for pid, entry in all_entries.items():
        entry_cwd = _norm_cwd(entry.get("cwd"))
        chain = parents.get(pid, 0)
        depth = 1
        seen = set()
        while chain and chain not in seen:
            if chain in roots:
                if entry_cwd == want_cwd[chain]:
                    out.setdefault(chain, []).append(
                        (depth, entry.get("updatedAt", 0), entry))
                break
            seen.add(chain)
            chain = parents.get(chain, 0)
            depth += 1
    return {root: min(cands, key=lambda c: (c[0], -c[1]))[2]
            for root, cands in out.items()}


def resolve_entry(entry: dict) -> dict | None:
    """登记 -> {"file", "title"}；sessionId 对应的 jsonl 尚不存在时返回 None
    （/clear 后首条事件落盘前的竞态：调用方保留时间匹配的旧答案，比显示空好）。
    """
    sid = entry.get("sessionId")
    cwd = entry.get("cwd")
    if not sid or not cwd:
        return None
    fname = f"{sid}.jsonl"
    for f in transcripts.list_files_cached(cwd):
        if f["file"] == fname:
            return {"file": fname, "title": transcripts._file_title(f)}
    return None
