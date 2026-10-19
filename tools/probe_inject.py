# -*- coding: utf-8 -*-
"""探针 2: AttachConsole + WriteConsoleInputW 跨控制台打字（纯 ctypes，x64）。"""
import ctypes as C
from ctypes import wintypes as W
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from probe_peb import snapshot, peb_info

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.FreeConsole.restype = W.BOOL
k32.AttachConsole.restype = W.BOOL
k32.AttachConsole.argtypes = [W.DWORD]
k32.GetStdHandle.restype = W.HANDLE
k32.GetStdHandle.argtypes = [W.DWORD]
k32.CreateFileW.restype = W.HANDLE
k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                            W.DWORD, W.DWORD, W.HANDLE]
k32.CloseHandle.argtypes = [W.HANDLE]
k32.WriteConsoleInputW.restype = W.BOOL

STD_INPUT_HANDLE = 0xFFFFFFF6
KEY_EVENT = 0x0001
VK_RETURN = 0x0D
LEFT_CTRL = 0x0008


class KEY_EVENT_RECORD(C.Structure):
    _fields_ = [
        ("bKeyDown", W.BOOL), ("wRepeatCount", W.WORD),
        ("wVirtualKeyCode", W.WORD), ("wVirtualScanCode", W.WORD),
        ("UnicodeChar", W.WCHAR), ("dwControlKeyState", W.DWORD),
    ]


class INPUT_RECORD(C.Structure):
    _fields_ = [("EventType", W.WORD), ("_pad", W.WORD), ("KeyEvent", KEY_EVENT_RECORD)]


k32.WriteConsoleInputW.argtypes = [W.HANDLE, C.POINTER(INPUT_RECORD), W.DWORD,
                                   C.POINTER(W.DWORD)]


def key_record(ch, down=1, vk=0, ctrl=0):
    return INPUT_RECORD(KEY_EVENT, 0, KEY_EVENT_RECORD(down, 1, vk, 0, ch, ctrl))


def build_records(text, submit=True):
    recs = []
    for ch in text:
        recs.append(key_record(ch, 1))
        recs.append(key_record(ch, 0))
    if submit:
        recs.append(key_record("\r", 1, VK_RETURN))
        recs.append(key_record("\r", 0, VK_RETURN))
    return recs


def inject(pid, text, submit=True):
    parent = os.getppid()
    had_console = k32.FreeConsole()
    try:
        if not k32.AttachConsole(pid):
            err = C.get_last_error()
            return False, f"AttachConsole({pid}) fail err={err}"
        # AttachConsole 后 GetStdHandle 可能仍指向旧句柄，规范做法是打开 CONIN$
        h = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if not h or h == W.HANDLE(-1).value:
            return False, f"CreateFile CONIN$ fail err={C.get_last_error()}"
        try:
            recs = build_records(text, submit)
            arr = (INPUT_RECORD * len(recs))(*recs)
            written = W.DWORD(0)
            ok = k32.WriteConsoleInputW(h, arr, len(recs), C.byref(written))
            if not ok:
                return False, f"WriteConsoleInput fail err={C.get_last_error()}"
            return True, f"written~{written.value}"
        finally:
            k32.CloseHandle(h)
    finally:
        k32.FreeConsole()
        if had_console:
            k32.AttachConsole(parent)


def find_target():
    for pid, ppid, name in snapshot():
        if name.lower() == "python.exe":
            info, _ = peb_info(pid)
            if info and "inj_target.py" in info["cmdline"]:
                return pid
    return None


if __name__ == "__main__":
    pid = find_target()
    print("target pid:", pid)
    if pid:
        for msg in ("HELLO_12345", "中文注入测试_Ω"):
            ok, r = inject(pid, msg)
            print(msg, "->", ok, r)
            time.sleep(0.5)
