# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server.winproc import snapshot, peb_info

needle = sys.argv[1]
for pid, _pp, nm in snapshot():
    if nm.lower() in ("node.exe", "python.exe", "claude.exe"):
        info = peb_info(pid)
        if info and needle in info["cmdline"]:
            print(pid)
