# -*- coding: utf-8 -*-
"""验证 pywinpty ConPTY 写入 CJK 是否重复（node raw 靶子）。"""
import sys
import time

import winpty

pty = winpty.PTY(100, 24)
ok = pty.spawn(r"C:\Windows\System32\cmd.exe",
               r'cmd /c chcp 65001 >nul && node E:\1workspace\claude-remote-web\tools\node_target.js')
print("spawn:", ok)
time.sleep(2.5)
# 清启动输出
pty.read(blocking=False)
pty.write("a中bΩ")
time.sleep(1.2)
out = pty.read(blocking=False)
print("screen echo:", repr(out[-200:]))
import pathlib
log = pathlib.Path(r"E:\1workspace\claude-remote-web\data\node_keys.log").read_text()
print("node got bytes:", log.strip().splitlines())
