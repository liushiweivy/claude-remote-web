# -*- coding: utf-8 -*-
"""枚举所有 claude.exe 各自附着的控制台窗口句柄，排查共享输入缓冲。"""
import ctypes as C
import os
import sys
from ctypes import wintypes as W

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server import sessions, winproc

k32 = C.WinDLL("kernel32", use_last_error=True)
u32 = C.WinDLL("user32")
k32.GetConsoleWindow.restype = W.HWND
parent = os.getppid()

for s in sessions.list_sessions():
    pid = s["pid"]
    k32.FreeConsole()
    attached = k32.AttachConsole(pid)
    hwnd = k32.GetConsoleWindow() if attached else None
    owner = W.DWORD()
    title = ""
    if hwnd:
        u32.GetWindowThreadProcessId(hwnd, C.byref(owner))
        buf = C.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value
    print(f"pid={pid:6d} attached={bool(attached)} conhwnd={hwnd or 0:#x} "
          f"owner={owner.value:6d} title={title!r} cwd={s['cwd']}")

k32.FreeConsole()
k32.AttachConsole(parent)
