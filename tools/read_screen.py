# -*- coding: utf-8 -*-
"""读目标控制台的屏幕缓冲区（ConPTY 客户端缓冲），打印可见文本。"""
import ctypes as C
import os
import sys
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)

GENERIC_RW = 0xC0000000
FILE_SHARE_RW = 3
OPEN_EXISTING = 3


class COORD(C.Structure):
    _fields_ = [("X", W.SHORT), ("Y", W.SHORT)]


class SMALL_RECT(C.Structure):
    _fields_ = [("Left", W.SHORT), ("Top", W.SHORT),
                ("Right", W.SHORT), ("Bottom", W.SHORT)]


class CONSOLE_SCREEN_BUFFER_INFO(C.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", W.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


class CHAR_INFO(C.Structure):
    _fields_ = [("UnicodeChar", W.WCHAR), ("Attributes", W.WORD)]


pid = int(sys.argv[1])
parent = os.getppid()
k32.FreeConsole()
assert k32.AttachConsole(pid), C.get_last_error()

h = k32.CreateFileW("CONOUT$", GENERIC_RW, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
assert h and h != -1, C.get_last_error()
k32.CreateFileW.restype = W.HANDLE

info = CONSOLE_SCREEN_BUFFER_INFO()
assert k32.GetConsoleScreenBufferInfo(h, C.byref(info))
w, ht = info.dwSize.X, info.dwSize.Y
print(f"buffer {w}x{ht} cursor=({info.dwCursorPosition.X},{info.dwCursorPosition.Y})")

CHAR_INFO_A = CHAR_INFO * w
buf = (CHAR_INFO_A * ht)()
read_region = SMALL_RECT(0, 0, w - 1, ht - 1)
ok = k32.ReadConsoleOutputW(h, buf, COORD(w, ht), COORD(0, 0), C.byref(read_region))
print("read ok:", ok, C.get_last_error())
lines = []
for y in range(ht):
    row = buf[y]
    line = "".join(row[x].UnicodeChar for x in range(w)).rstrip()
    if line.strip():
        lines.append(f"{y:3d}|{line}")
out = "\n".join(lines[-60:])
sys.stdout.reconfigure(encoding="utf-8")
print(out)
k32.CloseHandle(h)
k32.FreeConsole()
k32.AttachConsole(parent)
