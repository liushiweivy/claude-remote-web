# -*- coding: utf-8 -*-
"""定位包含指定 pid 的 Windows Terminal 窗口，前置并 Ctrl+Tab。"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server import winproc

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 15592
TABS = int(sys.argv[2]) if len(sys.argv) > 2 else 1

u = C.WinDLL("user32")
u.SetProcessDPIAware()

rows = winproc.snapshot()
parents = {p: pp for p, pp, _ in rows}


def is_descendant(pid, root):
    cur = pid
    for _ in range(12):
        if cur == root:
            return True
        cur = parents.get(cur, 0)
        if not cur:
            return False
    return False


EnumProc = C.WINFUNCTYPE(C.c_bool, W.HWND, C.c_void_p)
wins = []


def cb(h, _):
    buf = C.create_unicode_buffer(256)
    u.GetClassNameW(h, buf, 256)
    if buf.value == "CASCADIA_HOSTING_WINDOW_CLASS" and u.IsWindowVisible(h):
        pid = W.DWORD()
        u.GetWindowThreadProcessId(h, C.byref(pid))
        wins.append((h, pid.value))
    return True


u.EnumWindows(EnumProc(cb), 0)
target_hwnd = None
for h, wpid in wins:
    if is_descendant(TARGET, wpid):
        target_hwnd = h
        print(f"target WT hwnd={h:#x} wtpid={wpid}")
        break

if not target_hwnd:
    print("NOT FOUND; windows:", [(hex(h), p) for h, p in wins])
    sys.exit(1)

u.ShowWindow(target_hwnd, 9)  # restore
u.SetForegroundWindow(target_hwnd)
time.sleep(0.6)
for _ in range(TABS):
    u.keybd_event(0x11, 0, 0, 0)
    u.keybd_event(0x09, 0, 0, 0)
    time.sleep(0.05)
    u.keybd_event(0x09, 0, 2, 0)
    u.keybd_event(0x11, 0, 2, 0)
    time.sleep(0.5)
print("focused")
