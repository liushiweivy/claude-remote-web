# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import sessions, transcripts
from server.app import pty_manager

external = sessions.list_sessions(pty_manager.root_pids(), ttl=0)
sess = next(s for s in external if s["pid"] == 28440)
same_cwd = sess.get("cwd")
print("target cwd:", repr(same_cwd), "started:", sess.get("started"))
managed = pty_manager.list()
group = [{"key": ("ext", s["pid"]), "cwd": s.get("cwd"), "started": s.get("started")}
         for s in external if not s["managed"] and s.get("cwd") == same_cwd]
group += [{"key": ("mgr", m["id"]), "cwd": m.get("cwd"), "started": m.get("started")}
          for m in managed if m.get("cwd") == same_cwd]
for g in group:
    print(" group:", g)
res = transcripts.resolve_files(group)
for k, v in res.items():
    print("  resolved:", k, v)
