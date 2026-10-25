# -*- coding: utf-8 -*-
"""只读磁盘：E--1workspace 下最近 jsonl 的 first_ts / mtime / 末条用户消息。"""
import datetime
import glob
import json
import os

PROJ = os.path.expanduser("~/.claude/projects/E--1workspace")


def parse(path):
    first_ts = None
    last_user = None
    last_ts = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = o.get("timestamp")
            if ts:
                last_ts = ts
                if first_ts is None:
                    first_ts = ts
            if o.get("type") == "user":
                m = o.get("message", {})
                c = m.get("content")
                if isinstance(c, list):
                    parts = [x.get("text", "") for x in c
                             if isinstance(x, dict) and x.get("type") == "text"]
                    c = " ".join(parts)
                c = str(c or "").strip()
                if c and not c.startswith("<") and not c.startswith("↩"):
                    last_user = c[:70]
    return first_ts, last_ts, last_user


rows = []
for f in glob.glob(os.path.join(PROJ, "*.jsonl")):
    st = os.stat(f)
    rows.append((st.st_mtime, st.st_size, os.path.basename(f), f))
rows.sort(reverse=True)

now = datetime.datetime.now()
for mt, size, name, path in rows[:14]:
    first_ts, last_ts, last_user = parse(path)
    age = (now - datetime.datetime.fromtimestamp(mt)).total_seconds()
    ft = (first_ts or "")[:19].replace("T", " ")
    lt = (last_ts or "")[:19].replace("T", " ")
    print(f"age={age/60:7.1f}m size={size:>9} first={ft} last={lt} {name[:13]}")
    print(f"          last_user={last_user}")
