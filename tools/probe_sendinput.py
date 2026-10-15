# -*- coding: utf-8 -*-
"""SendInput KEYEVENTF_UNICODE 探针：聚焦目标控制台窗口后系统级注入 Unicode。"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server import winproc

u32 = C.WinDLL("user32", use_last_error=True)
k32 = C.WinDLL("kernel32", use_last_error=True)
k32.GetConsoleWindow.restype = W.HWND

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x4
KEYEVENTF_KEYUP = 0x2


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", C.POINTER(C.c_ulong))]


class _INNER(C.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", C.c_char * 32)]


class INPUT(C.Structure):
    _fields_ = [("type", W.DWORD), ("u", _INNER)]


def send_unicode(text: str, submit: bool = True):
    recs = []
    for ch in text:
        code = ord(ch)
        d = _INNER(); d.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)
        recs.append(INPUT(INPUT_KEYBOARD, d))
        u2 = _INNER(); u2.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
        recs.append(INPUT(INPUT_KEYBOARD, u2))
    if submit:
        for flags in (0, KEYEVENTF_KEYUP):
            d = _INNER(); d.ki = KEYBDINPUT(0x0D, 0, flags, 0, None)
            recs.append(INPUT(INPUT_KEYBOARD, d))
    arr = (INPUT * len(recs))(*recs)
    n = u32.SendInput(len(recs), arr, C.sizeof(INPUT))
    return n, C.get_last_error()


pid = int(sys.argv[1])
text = sys.argv[2] if len(sys.argv) > 2 else "系统级输入午未申Ω"

parent = C.windll.kernel32.GetCurrentProcessId
# 找目标控制台窗口：AttachConsole 拿 hwnd
k32.FreeConsole()
assert k32.AttachConsole(pid)
hwnd = k32.GetConsoleWindow()
k32.FreeConsole()
k32.AttachConsole(C.windll.kernel32.GetCurrentProcessId())
print("console hwnd:", hex(hwnd or 0))
if hwnd:
    u32.ShowWindow(hwnd, 9)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
print("send:", send_unicode(text))
