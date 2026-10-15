# -*- coding: utf-8 -*-
"""Windows 进程枚举 + PEB 读取（cwd / 命令行）。纯 ctypes，无 PowerShell 冷启动。

只支持 x64。偏移来自实测 dump（RTL_USER_PROCESS_PARAMETERS）：
ProcessParameters 在 PEB+0x20；CurrentDirectorys@0x38、ImagePathName@0x60、
CommandLine@0x70（UNICODE_STRING 的 Buffer 均在结构内 +8）。
"""
from __future__ import annotations

import ctypes as C
from datetime import datetime, timezone
from ctypes import wintypes as W

ntdll = C.WinDLL("ntdll")
k32 = C.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32.CreateToolhelp32Snapshot.restype = W.HANDLE
k32.Process32FirstW.argtypes = [W.HANDLE, C.c_void_p]
k32.Process32NextW.argtypes = [W.HANDLE, C.c_void_p]
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.CloseHandle.argtypes = [W.HANDLE]


class PROCESSENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("cntUsage", W.DWORD),
        ("th32ProcessID", W.DWORD), ("th32DefaultHeapID", C.POINTER(C.c_ulong)),
        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", C.c_long),
        ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260),
    ]


class UNICODE_STRING(C.Structure):
    _fields_ = [("Length", W.USHORT), ("MaxLength", W.USHORT),
                ("_pad", W.ULONG), ("Buffer", C.c_void_p)]


class PROCESS_BASIC_INFORMATION(C.Structure):
    _fields_ = [("ExitStatus", C.c_longlong), ("PebBaseAddress", C.c_void_p),
                ("AffinityMask", C.c_size_t), ("BasePriority", C.c_long),
                ("_pad", W.DWORD), ("UniqueProcessId", C.c_size_t),
                ("InheritedFromUniqueProcessId", C.c_size_t)]


def snapshot() -> list[tuple[int, int, str]]:
    """返回 (pid, ppid, exe_name) 全量列表。"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32W()
    pe.dwSize = C.sizeof(pe)
    rows: list[tuple[int, int, str]] = []
    if k32.Process32FirstW(snap, C.byref(pe)):
        while True:
            rows.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.szExeFile))
            if not k32.Process32NextW(snap, C.byref(pe)):
                break
    k32.CloseHandle(snap)
    return rows


def _read(h, addr: int, size: int) -> bytes:
    buf = (C.c_char * size)()
    got = C.c_size_t()
    ok = k32.ReadProcessMemory(h, C.c_void_p(addr), buf, size, C.byref(got))
    return buf.raw[:got.value] if ok else b""


def _read_unicode(h, usp: UNICODE_STRING) -> str:
    if not usp.Buffer or not usp.Length:
        return ""
    return _read(h, usp.Buffer, usp.Length).decode("utf-16-le", "replace")


def peb_info(pid: int) -> dict | None:
    """返回 {cwd, image, cmdline}；读不到（权限/进程退出）返回 None。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        retlen = C.c_ulong()
        st = ntdll.NtQueryInformationProcess(
            h, 0, C.byref(pbi), C.sizeof(pbi), C.byref(retlen))
        if st != 0 or not pbi.PebBaseAddress:
            return None
        raw = _read(h, pbi.PebBaseAddress + 0x20, 8)
        pp = int.from_bytes(raw, "little")
        if not pp:
            return None
        cur = UNICODE_STRING.from_buffer_copy(_read(h, pp + 0x38, 16))
        img = UNICODE_STRING.from_buffer_copy(_read(h, pp + 0x60, 16))
        cmd = UNICODE_STRING.from_buffer_copy(_read(h, pp + 0x70, 16))
        return {"cwd": _read_unicode(h, cur).rstrip("\\"),
                "image": _read_unicode(h, img),
                "cmdline": _read_unicode(h, cmd)}
    finally:
        k32.CloseHandle(h)


def process_started(pid: int) -> datetime | None:
    """进程创建时间（本地时区）。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    try:
        created, _x, _y, _z = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
        if not k32.GetProcessTimes(h, C.byref(created), C.byref(_x),
                                   C.byref(_y), C.byref(_z)):
            return None
        ft = created
        total = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return datetime.fromtimestamp(total / 1e7 - 11644473600, tz=timezone.utc).astimezone()
    finally:
        k32.CloseHandle(h)
