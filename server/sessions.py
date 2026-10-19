# -*- coding: utf-8 -*-
"""本机 Claude 会话发现：聚合进程快照、PEB 信息与托管会话标记。"""
from __future__ import annotations

import re
import threading
import time

from . import winproc

# 原生安装版：...\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe
# 旧 npm 版：node.exe ... @anthropic-ai\claude-code\cli.js
RE_EXE = re.compile(r"@anthropic-ai[\\/]claude-code", re.IGNORECASE)
RE_NODE_CLI = re.compile(r"@anthropic-ai[\\/]claude-code[\\/].*cli\.js", re.IGNORECASE)
SELF_TAG = "claude-remote-web"  # 绝不把本服务自身误判为会话

SNAP_TTL_SEC = 2.0  # /api/sessions(5s) 与 /api/transcript(2.5s) 共用进程快照
_info_lock = threading.Lock()
_info_cache: dict = {"ts": 0.0, "rows": []}


def is_claude(name: str, image: str, cmdline: str) -> bool:
    hay = f"{image} {cmdline}"
    if SELF_TAG in hay:
        return False
    if name.lower() == "claude.exe" and RE_EXE.search(hay):
        return True
    if name.lower() == "node.exe" and RE_NODE_CLI.search(cmdline or ""):
        return True
    return False


def _snapshot_infos(ttl: float) -> list[tuple]:
    """[(pid, ppid, name, peb_info|None, started|None)]，带短 TTL 缓存。

    PEB/env/创建时间读取是本模块最重的操作（每个候选进程一次跨进程读），
    两个轮询端点共用缓存；ttl<=0 时永远取新（测试用，保证 mock 确定性）。
    """
    now = time.monotonic()
    with _info_lock:
        if ttl > 0 and _info_cache["rows"] and now - _info_cache["ts"] < ttl:
            return _info_cache["rows"]
    rows = winproc.snapshot()
    infos = []
    for pid, ppid, name in rows:
        if name.lower() in ("claude.exe", "node.exe"):
            info = winproc.peb_info(pid)
            started = winproc.process_started(pid) if info else None
        else:
            info, started = None, None
        infos.append((pid, ppid, name, info, started))
    if ttl > 0:
        with _info_lock:
            _info_cache["ts"] = now
            _info_cache["rows"] = infos
    return infos


def list_sessions(managed_roots: set[int] | None = None,
                  ttl: float = 0.0) -> list[dict]:
    """返回当前全部 claude 会话（外部 + 本服务托管）。

    managed_roots: 托管 PTY 直接 spawn 的根进程 pid 集合（cmd.exe），
    其子进程链上的 claude.exe 标记为 managed。
    ttl>0 时复用 SNAP_TTL_SEC 内的进程快照（线上轮询路径）。
    """
    managed_roots = managed_roots or set()
    infos = _snapshot_infos(ttl)
    parents = {pid: ppid for pid, ppid, _n, _i, _s in infos}
    out: list[dict] = []
    for pid, ppid, name, info, started in infos:
        if info is None:
            continue
        if not is_claude(name, info.get("image", ""), info.get("cmdline", "")):
            continue
        # Task/Agent 拉起的子会话非交互、通常无自己的 jsonl，出现必与父会话
        # 撞标题；best-effort 过滤（env 读不到时不误伤）。
        if info.get("env", {}).get("CLAUDE_CODE_CHILD_SESSION"):
            continue
        managed = pid in managed_roots
        # 沿父链一直查到根（cmd -> claude.cmd -> 多层 cmd -> claude.exe），
        # 旧实现固定 4 层在深链时漏标，漏标的托管进程会同时出现在外部列表。
        chain = ppid
        seen = set()
        while chain and chain not in seen:
            if chain in managed_roots:
                managed = True
                break
            seen.add(chain)
            chain = parents.get(chain, 0)
        out.append({
            "pid": pid,
            "ppid": ppid,
            "name": name,
            "cwd": info["cwd"] or None,
            "cmdline": info["cmdline"],
            "started": started.isoformat() if started else None,
            "managed": managed,
            # 微信桥 spawn 时打的 WECHAT_BRIDGE=1 标记：前端显示「微信桥」来源标签
            "bridge": bool(info.get("env", {}).get("WECHAT_BRIDGE")),
        })
    out.sort(key=lambda s: (not s["managed"], s.get("started") or ""))
    return out


def session_by_pid(pid: int) -> dict | None:
    for sess in list_sessions():
        if sess["pid"] == pid:
            return sess
    return None
