# -*- coding: utf-8 -*-
"""本机 Claude 会话发现：聚合进程快照、PEB 信息与托管会话标记。"""
from __future__ import annotations

import os
import re
from datetime import datetime

from . import winproc

# 原生安装版：...\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe
# 旧 npm 版：node.exe ... @anthropic-ai\claude-code\cli.js
RE_EXE = re.compile(r"@anthropic-ai[\\/]claude-code", re.IGNORECASE)
RE_NODE_CLI = re.compile(r"@anthropic-ai[\\/]claude-code[\\/].*cli\.js", re.IGNORECASE)
SELF_TAG = "claude-remote-web"  # 绝不把本服务自身误判为会话


def is_claude(name: str, image: str, cmdline: str) -> bool:
    hay = f"{image} {cmdline}"
    if SELF_TAG in hay:
        return False
    if name.lower() == "claude.exe" and RE_EXE.search(hay):
        return True
    if name.lower() == "node.exe" and RE_NODE_CLI.search(cmdline or ""):
        return True
    return False


def list_sessions(managed_roots: set[int] | None = None) -> list[dict]:
    """返回当前全部 claude 会话（外部 + 本服务托管）。

    managed_roots: 托管 PTY 直接 spawn 的根进程 pid 集合（cmd.exe），
    其子进程链上的 claude.exe 标记为 managed。
    """
    managed_roots = managed_roots or set()
    rows = winproc.snapshot()
    parents = {pid: ppid for pid, ppid, _ in rows}
    out: list[dict] = []
    for pid, ppid, name in rows:
        if name.lower() not in ("claude.exe", "node.exe"):
            continue
        info = winproc.peb_info(pid)
        if not info or not is_claude(name, info.get("image", ""), info.get("cmdline", "")):
            continue
        started = winproc.process_started(pid)
        managed = pid in managed_roots
        chain = ppid
        for _ in range(4):  # cmd -> claude.cmd -> claude.exe，向上查几层
            if chain in managed_roots:
                managed = True
                break
            chain = parents.get(chain, 0)
        out.append({
            "pid": pid,
            "ppid": ppid,
            "name": name,
            "cwd": info["cwd"] or None,
            "cmdline": info["cmdline"],
            "started": started.isoformat() if started else None,
            "managed": managed,
        })
    out.sort(key=lambda s: (not s["managed"], s.get("started") or ""))
    return out


def session_by_pid(pid: int) -> dict | None:
    for sess in list_sessions():
        if sess["pid"] == pid:
            return sess
    return None
