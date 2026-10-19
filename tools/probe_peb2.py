# -*- coding: utf-8 -*-
"""探针 1b: dump 自身 PEB->ProcessParameters 前 0x90 字节，定位偏移。"""
import ctypes as C
from ctypes import wintypes as W
import os

ntdll = C.WinDLL("ntdll")
k32 = C.WinDLL("kernel32", use_last_error=True)


class PBI(C.Structure):
    _fields_ = [("ExitStatus", C.c_longlong), ("PebBaseAddress", C.c_void_p),
                ("AffinityMask", C.c_size_t), ("BasePriority", C.c_long),
                ("_pad", W.DWORD), ("UniqueProcessId", C.c_size_t),
                ("InheritedFromUniqueProcessId", C.c_size_t)]


k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
h = k32.OpenProcess(0x0400 | 0x0010, False, os.getpid())
pbi = PBI()
rl = C.c_ulong()
st = ntdll.NtQueryInformationProcess(h, 0, C.byref(pbi), C.sizeof(pbi), C.byref(rl))
print("status", f"0x{st & 0xffffffff:08x}", "size", C.sizeof(pbi), "peb", pbi.PebBaseAddress, "pid", pbi.UniqueProcessId, "self", os.getpid())


def rd(addr, n):
    buf = (C.c_char * n)()
    got = C.c_size_t()
    ok = k32.ReadProcessMemory(h, C.c_void_p(addr), buf, n, C.byref(got))
    if not ok:
        print("RPM fail", C.get_last_error(), "at", hex(addr))
        return b""
    return buf.raw[:got.value]


pp_raw = rd(pbi.PebBaseAddress + 0x20, 8)
pp = int.from_bytes(pp_raw, "little")
print("ProcessParameters ptr:", hex(pp))
blob = rd(pp, 0x90)
for i in range(0, len(blob), 16):
    hx = " ".join(f"{b:02x}" for b in blob[i:i+16])
    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in blob[i:i+16])
    print(f"{i:04x}  {hx:<47} {asc}")
