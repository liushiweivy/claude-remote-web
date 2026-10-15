# -*- coding: utf-8 -*-
"""跨控制台输入注入：AttachConsole + CONIN$ + WriteConsoleInputW。

已实测对传统 conhost 与 Windows Terminal（ConPTY）均有效，支持 cooked/raw 模式、
中文与 Ω 等非 ASCII 字符（与 Claude/Ink 的 ReadConsoleInput 读取方式等价）。

注意：AttachConsole 是进程级全局状态，所有调用必须持 _LOCK；
完成后 FreeConsole 并重新挂回本服务自身控制台。
"""
from __future__ import annotations

import ctypes as C
import os
import threading
from ctypes import wintypes as W

from . import winproc

k32 = C.WinDLL("kernel32", use_last_error=True)
u32 = C.WinDLL("user32")

STD_ERROR_HANDLE = 0xFFFFFFF4  # 仅用于探测本进程是否原本挂着控制台
KEY_EVENT = 0x0001
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
LEFT_CTRL_PRESSED = 0x0008
GENERIC_RW = 0xC0000000
FILE_SHARE_RW = 0x00000003
OPEN_EXISTING = 3
INVALID = C.c_void_p(-1).value

k32.FreeConsole.restype = W.BOOL
k32.AttachConsole.restype = W.BOOL
k32.AttachConsole.argtypes = [W.DWORD]
k32.CreateFileW.restype = W.HANDLE
k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                            W.DWORD, W.DWORD, W.HANDLE]
k32.CloseHandle.argtypes = [W.HANDLE]
k32.GetStdHandle.restype = W.HANDLE
k32.GetStdHandle.argtypes = [W.DWORD]
k32.WriteConsoleInputW.restype = W.BOOL
k32.GetConsoleWindow.restype = W.HWND
u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]

_LOCK = threading.Lock()
_ORIG_PARENT = os.getppid()
_ORIG_HAS_CONSOLE = k32.GetStdHandle(STD_ERROR_HANDLE) not in (0, INVALID)


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


def char_records(ch: str, vk: int = 0, ctrl: int = 0) -> list[INPUT_RECORD]:
    # 真实硬件：抬起事件的 UnicodeChar 为 0。Node/libuv 对 vk=0 的 Unicode
    # 字符会同时消费 down/up，抬起若带字符会导致中文等非 ASCII 字符重复。
    down = INPUT_RECORD(KEY_EVENT, 0, KEY_EVENT_RECORD(1, 1, vk, 0, ch, ctrl))
    up = INPUT_RECORD(KEY_EVENT, 0, KEY_EVENT_RECORD(0, 1, vk, 0, "\0", ctrl))
    return [down, up]


def enter_records() -> list[INPUT_RECORD]:
    return char_records("\r", VK_RETURN)


def build_send_records(text: str, submit: bool = True) -> list[INPUT_RECORD]:
    """多行文本走 bracketed-paste（ESC[200~ ... ESC[201~），Claude/Ink 支持。"""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in body:
        seq = "\x1b[200~" + body + "\x1b[201~"
    else:
        seq = body
    recs: list[INPUT_RECORD] = []
    for ch in seq:
        recs.extend(char_records(ch))
    if submit:
        recs.extend(enter_records())
    return recs


def control_key(kind: str) -> list[INPUT_RECORD]:
    if kind == "escape":
        return char_records("\x1b", VK_ESCAPE)
    if kind == "ctrl_c":
        return char_records("\x03", 0x43, LEFT_CTRL_PRESSED)
    if kind == "enter":
        return enter_records()
    raise ValueError(f"unknown control key: {kind}")


def _restore_console() -> None:
    k32.FreeConsole()
    if _ORIG_HAS_CONSOLE:
        k32.AttachConsole(_ORIG_PARENT)


def probe(pid: int) -> dict:
    """探测能否向目标控制台注入，顺带取窗口标题/宿主类型。"""
    with _LOCK:
        k32.FreeConsole()
        if not k32.AttachConsole(pid):
            err = C.get_last_error()
            _restore_console()
            return {"ok": False, "error": f"AttachConsole 失败 (err={err})",
                    "injectable": False}
        hwnd = k32.GetConsoleWindow()
        title = ""
        owner_pid = 0
        if hwnd:
            buf = C.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            title = buf.value
            owner_pid_w = W.DWORD()
            u32.GetWindowThreadProcessId(hwnd, C.byref(owner_pid_w))
            owner_pid = owner_pid_w.value
        h = k32.CreateFileW("CONIN$", GENERIC_RW, FILE_SHARE_RW, None,
                            OPEN_EXISTING, 0, None)
        ok = bool(h) and h != INVALID
        err = None if ok else f"CONIN$ 打开失败 (err={C.get_last_error()})"
        if ok:
            k32.CloseHandle(h)
        _restore_console()
        owner_name = ""
        for p, _pp, nm in winproc.snapshot():
            if p == owner_pid:
                owner_name = nm
                break
        return {"ok": True, "injectable": ok, "error": err, "title": title,
                "window_owner": owner_name or None, "conpty": owner_name.lower() != "conhost.exe"}


def send_records(pid: int, records: list[INPUT_RECORD]) -> dict:
    if not records:
        return {"ok": False, "error": "空输入"}
    with _LOCK:
        k32.FreeConsole()
        if not k32.AttachConsole(pid):
            err = C.get_last_error()
            _restore_console()
            return {"ok": False, "error": f"AttachConsole 失败 (err={err})"}
        h = k32.CreateFileW("CONIN$", GENERIC_RW, FILE_SHARE_RW, None,
                            OPEN_EXISTING, 0, None)
        if not h or h == INVALID:
            err = C.get_last_error()
            _restore_console()
            return {"ok": False, "error": f"CONIN$ 打开失败 (err={err})"}
        try:
            arr = (INPUT_RECORD * len(records))(*records)
            written = W.DWORD(0)
            ok = k32.WriteConsoleInputW(h, arr, len(records), C.byref(written))
            if not ok:
                return {"ok": False, "error": f"WriteConsoleInput 失败 (err={C.get_last_error()})"}
            return {"ok": True, "events": written.value}
        finally:
            k32.CloseHandle(h)
            _restore_console()


def send_text(pid: int, text: str, submit: bool = True) -> dict:
    if not text:
        return {"ok": False, "error": "文本为空"}
    return send_records(pid, build_send_records(text, submit))
