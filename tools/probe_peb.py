# -*- coding: utf-8 -*-
"""探针 1: ctypes 枚举进程 + 读 PEB 拿 cwd / 命令行（x64）。"""
import ctypes as C
from ctypes import wintypes as W

ntdll = C.WinDLL("ntdll")
k32 = C.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
ProcessBasicInformation = 0


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
                ("pad", W.ULONG), ("Buffer", C.c_void_p)]


class PROCESS_BASIC_INFORMATION(C.Structure):
    _fields_ = [("ExitStatus", C.c_longlong), ("PebBaseAddress", C.c_void_p),
                ("AffinityMask", C.c_size_t), ("BasePriority", C.c_long),
                ("UniqueProcessId", C.c_size_t), ("InheritedFromUniqueProcessId", C.c_size_t)]


def snapshot():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32W()
    pe.dwSize = C.sizeof(pe)
    rows = []
    if k32.Process32FirstW(snap, C.byref(pe)):
        while True:
            rows.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.szExeFile))
            if not k32.Process32NextW(snap, C.byref(pe)):
                break
    k32.CloseHandle(snap)
    return rows


def read(h, addr, n):
    buf = (C.c_char * n)()
    nread = C.c_size_t()
    ok = k32.ReadProcessMemory(h, C.c_void_p(addr), buf, n, C.byref(nread))
    return buf.raw[:nread.value] if ok else b""


def read_unicode(h, usp):
    if not usp.Buffer or usp.Length == 0:
        return ""
    raw = read(h, usp.Buffer, usp.Length)
    return raw.decode("utf-16-le", "replace")


k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]


def peb_info(pid):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, f"OpenProcess err={C.get_last_error()}"
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        retlen = C.c_ulong()
        st = ntdll.NtQueryInformationProcess(
            h, ProcessBasicInformation, C.byref(pbi), C.sizeof(pbi), C.byref(retlen))
        if st != 0:
            return None, f"NtQuery st=0x{st & 0xffffffff:08x}"
        # PEB x64: ProcessParameters @ 0x20
        pp_ptr = C.c_void_p()
        raw = read(h, pbi.PebBaseAddress + 0x20, 8)
        pp_addr = int.from_bytes(raw, "little")
        if not pp_addr:
            return None, "no ProcessParameters"
        # RTL_USER_PROCESS_PARAMETERS x64 (实测):
        # 0x38 CurrentDirectorys UNICODE_STRING(Buffer@0x40) + HANDLE@0x48
        # 0x60 ImagePathName / 0x70 CommandLine
        cur = UNICODE_STRING.from_buffer_copy(read(h, pp_addr + 0x38, 16))
        img = UNICODE_STRING.from_buffer_copy(read(h, pp_addr + 0x60, 16))
        cmd = UNICODE_STRING.from_buffer_copy(read(h, pp_addr + 0x70, 16))
        return {
            "cwd": read_unicode(h, cur).rstrip("\\"),
            "image": read_unicode(h, img),
            "cmdline": read_unicode(h, cmd),
        }, None
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    import os, sys
    info, err = peb_info(os.getpid())
    print("SELF:", info, err)
    for pid, ppid, name in snapshot():
        if name.lower() in ("claude.exe",):
            info, err = peb_info(pid)
            print(f"claude pid={pid} ppid={ppid}: {info or err}")
