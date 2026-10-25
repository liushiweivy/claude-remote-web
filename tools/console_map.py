# -*- coding: utf-8 -*-
"""临时诊断：列出每个 claude.exe / 父 cmd 实际附加的控制台 HWND，找共享控制台。"""
import ctypes as C
from ctypes import wintypes as W

from server import winproc

k32 = C.WinDLL("kernel32", use_last_error=True)
u32 = C.WinDLL("user32")
k32.FreeConsole.restype = W.BOOL
k32.AttachConsole.restype = W.BOOL
k32.AttachConsole.argtypes = [W.DWORD]
k32.GetConsoleWindow.restype = W.HWND
u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]

rows = winproc.snapshot()
parents = {p: pp for p, pp, _ in rows}
names = {p: n for p, _pp, n in rows}

targets = [(p, n) for p, _pp, n in rows if n.lower() in ("claude.exe", "cmd.exe", "powershell.exe", "node.exe")]

def console_hwnd(pid):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return None, f"attach-err={C.get_last_error()}"
    hwnd = k32.GetConsoleWindow() or 0
    buf = C.create_unicode_buffer(256)
    u32.GetWindowTextW(hwnd, buf, 256)
    return hwnd, buf.value

for pid, name in sorted(targets, key=lambda x: x[1]):
    hwnd, title = console_hwnd(pid)
    ppid = parents.get(pid)
    print(f"{name:14s} pid={pid:<6} ppid={ppid:<6} hwnd={hwnd} title={title!r}")
