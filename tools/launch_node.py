# -*- coding: utf-8 -*-
import subprocess
subprocess.Popen(
    ["cmd", "/k", "node", r"E:\1workspace\claude-remote-web\tools\node_target.js"],
    creationflags=0x10)
print("launched")
