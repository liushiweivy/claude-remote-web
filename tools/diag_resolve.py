# -*- coding: utf-8 -*-
"""诊断：pid -> jsonl 归属。对比 resolver 输出 与 磁盘真实文件。"""
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import sessions, transcripts, winproc

PROJ = os.path.expanduser("~/.claude/projects/E--1workspace")


def file_first_event(path):
    first_ts = None
    first_user = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = o.get("timestamp")
                if ts and first_ts is None:
                    first_ts = ts
                if o.get("type") == "user" and first_user is None:
                    m = o.get("message", {})
                    c = m.get("content")
                    if isinstance(c, list):
                        c = " ".join(
                            x.get("text", "") for x in c if isinstance(x, dict)
                        )
                    c = str(c)
                    if c and not c.startswith("<"):
                        first_user = c[:60]
    except Exception:
        pass
    return first_ts, first_user


def main():
    procs = sessions.list_sessions()
    print("=== live procs ===")
    proc_objs = []
    for s in procs:
        print(
            f"pid={s['pid']:<6} start={s.get('started','')[:19]} managed={s['managed']} "
            f"resolved={s.get('file')}"
        )
        proc_objs.append(
            {
                "key": ("mgr" if s["managed"] else "ext", s.get("id") if s["managed"] else s["pid"]),
                "pid": s["pid"],
                "cwd": s.get("cwd"),
                "started": s.get("started"),
            }
        )

    res = transcripts.resolve_files(proc_objs, None)
    print("\n=== resolver result ===")
    for key, v in (res or {}).items():
        print(key, "->", v)

    print("\n=== disk: recent files in E--1workspace ===")
    rows = []
    for f in glob.glob(os.path.join(PROJ, "*.jsonl")):
        st = os.stat(f)
        ts, fu = file_first_event(f)
        rows.append((st.st_mtime, os.path.basename(f), ts, fu))
    rows.sort(reverse=True)
    for mt, f, ts, fu in rows[:12]:
        print(
            f"mtime={datetime.datetime.fromtimestamp(mt).strftime('%m-%d %H:%M')} "
            f"first={(ts or '')[:19]} {f[:13]} | {fu}"
        )


if __name__ == "__main__":
    main()
