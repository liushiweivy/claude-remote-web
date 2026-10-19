# -*- coding: utf-8 -*-
"""列出 cwd 下所有 jsonl 的 first_ts/mtime/标题，复算进程归属。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import transcripts

cwd = r"C:\Users\4v"
files = transcripts.list_files(cwd)
for f in sorted(files, key=lambda x: x.get("first_ts") or ""):
    print(f["file"][:8], "first_ts=", f.get("first_ts"),
          "mtime=", f.get("mtime"), "title=", (f.get("title") or "")[:20].encode("unicode_escape").decode())

procs = [
    (26280, "2026-10-16T08:34:28.807198+08:00"),
    (28440, "2026-10-16T08:35:34.650579+08:00"),
    (3528,  "2026-10-16T08:36:17.142422+08:00"),
    (25936, "2026-10-16T08:38:56.110935+08:00"),
    (10064, "2026-10-16T08:47:13.163416+08:00"),
]
group = [{"key": ("ext", pid), "cwd": cwd, "started": st} for pid, st in procs]
res = transcripts.resolve_files(group)
for pid, _ in procs:
    r = res[("ext", pid)]
    print(pid, "->", (r["file"] or "None")[:8], (r["title"] or "").encode("unicode_escape").decode()[:30])
