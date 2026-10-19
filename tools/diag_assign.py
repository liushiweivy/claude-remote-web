# -*- coding: utf-8 -*-
"""诊断：用修复后的 resolve_files 解析线上真实进程，核对归属。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import sessions, transcripts  # noqa: E402
from server.app import pty_manager  # noqa: E402

rows = sessions.list_sessions(pty_manager.root_pids())
procs = [{"key": ("ext", s["pid"]), "cwd": s.get("cwd"),
          "started": s.get("started")} for s in rows if not s["managed"]]
procs += [{"key": ("mgr", m["id"]), "cwd": m.get("cwd"),
           "started": m.get("started")} for m in pty_manager.list()]

res = transcripts.resolve_files(procs)
for s in rows:
    r = res.get(("ext", s["pid"]), {}) if not s["managed"] else {}
    tag = "MGR" if s["managed"] else "ext"
    print(f"pid {s['pid']} {tag} started={s['started'][11:19]} cwd={s.get('cwd')}")
    print(f"    -> file={(r.get('file') or '(无关联)')[:13]} title={r.get('title')}")
