# -*- coding: utf-8 -*-
"""探针：读 claude.exe 进程 PEB 环境块，验证 CLAUDE_CODE_SESSION_ID 是否存在，
以及 jsonl 文件名(uuid) 是否与之一致。用法: python probe_env.py <pid> [<pid> ...]"""
from __future__ import annotations

import ctypes as C
import sys
from ctypes import wintypes as W
from pathlib import Path

ntdll = C.WinDLL("ntdll")
k32 = C.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.CloseHandle.argtypes = [W.HANDLE]


class UNICODE_STRING(C.Structure):
    _fields_ = [("Length", W.USHORT), ("MaxLength", W.USHORT),
                ("_pad", W.ULONG), ("Buffer", C.c_void_p)]


class PBI(C.Structure):
    _fields_ = [("ExitStatus", C.c_longlong), ("PebBaseAddress", C.c_void_p),
                ("AffinityMask", C.c_size_t), ("BasePriority", C.c_long),
                ("_pad", W.DWORD), ("UniqueProcessId", C.c_size_t),
                ("InheritedFromUniqueProcessId", C.c_size_t)]


def read(h, addr, size):
    buf = (C.c_char * size)()
    got = C.c_size_t()
    ok = k32.ReadProcessMemory(h, C.c_void_p(addr), buf, size, C.byref(got))
    return buf.raw[:got.value] if ok else b""


WANTED = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
          "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE", "CLAUDE_PID", "AI_AGENT")


def probe(pid: int) -> None:
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"pid {pid}: OpenProcess 失败 err={C.get_last_error()}")
        return
    try:
        pbi = PBI()
        retlen = C.c_ulong()
        if ntdll.NtQueryInformationProcess(h, 0, C.byref(pbi), C.sizeof(pbi),
                                           C.byref(retlen)) != 0:
            print(f"pid {pid}: NtQuery 失败")
            return
        pp = int.from_bytes(read(h, pbi.PebBaseAddress + 0x20, 8), "little")
        if not pp:
            print(f"pid {pid}: 无 ProcessParameters")
            return
        env_addr = int.from_bytes(read(h, pp + 0x80, 8), "little")
        env_size = int.from_bytes(read(h, pp + 0x3F0, 8), "little")  # EnvironmentSize
        size = min(env_size or 256 * 1024, 512 * 1024)
        raw = read(h, env_addr, size)
        text = raw.decode("utf-16-le", "ignore")
        entries = text.split("\x00")
        found = {k: None for k in WANTED}
        for e in entries:
            for k in WANTED:
                if e.startswith(k + "="):
                    found[k] = e.split("=", 1)[1]
        print(f"--- pid {pid} (env {len(raw)} bytes / size={env_size}) ---")
        for k, v in found.items():
            print(f"  {k} = {v}")
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    pids = [int(x) for x in sys.argv[1:]]
    if not pids:
        # 默认枚举所有 claude.exe
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)

        class PE(C.Structure):
            _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                        ("th32ProcessID", W.DWORD),
                        ("th32DefaultHeapID", C.POINTER(C.c_ulong)),
                        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                        ("th32ParentProcessID", W.DWORD),
                        ("pcPriClassBase", C.c_long), ("dwFlags", W.DWORD),
                        ("szExeFile", W.WCHAR * 260)]

        pe = PE(); pe.dwSize = C.sizeof(pe)
        if k32.Process32FirstW(snap, C.byref(pe)):
            while True:
                if pe.szExeFile.lower() == "claude.exe":
                    pids.append(pe.th32ProcessID)
                if not k32.Process32NextW(snap, C.byref(pe)):
                    break
        k32.CloseHandle(snap)
    for pid in pids:
        probe(pid)
    proj = Path.home() / ".claude" / "projects"
    print("\n=== 最近 jsonl (按 mtime) ===")
    files = sorted(proj.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:10]
    for f in files:
        print(f"  {f.parent.name}/{f.name}")
