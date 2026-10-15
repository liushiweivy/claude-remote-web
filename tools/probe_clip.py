# -*- coding: utf-8 -*-
"""验证剪贴板+Ctrl+V 注入：CJK 不重复（原型，不做剪贴板备份）。"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server import injector

k32 = C.WinDLL("kernel32", use_last_error=True)
u32 = C.WinDLL("user32", use_last_error=True)
k32.GlobalAlloc.restype = W.HGLOBAL
k32.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
k32.GlobalLock.restype = C.c_void_p
k32.GlobalLock.argtypes = [W.HGLOBAL]
k32.GlobalUnlock.argtypes = [W.HGLOBAL]
u32.OpenClipboard.argtypes = [W.HWND]
u32.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
u32.GetClipboardData.restype = W.HANDLE
GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

pid = int(sys.argv[1])
text = sys.argv[2] if len(sys.argv) > 2 else "剪贴板注入测试戊己庚Ω"

assert u32.OpenClipboard(0)
u32.EmptyClipboard()
buf = C.create_unicode_buffer(text + "\0")
h = k32.GlobalAlloc(GMEM_MOVEABLE, C.sizeof(buf))
ptr = k32.GlobalLock(h)
C.memmove(ptr, buf, C.sizeof(buf))
k32.GlobalUnlock(h)
u32.SetClipboardData(CF_UNICODETEXT, h)
u32.CloseClipboard()
time.sleep(0.2)

VK_V = 0x56
recs = [
    injector.INPUT_RECORD(injector.KEY_EVENT, 0,
                         injector.KEY_EVENT_RECORD(1, 1, VK_V, 0, "\x16", injector.LEFT_CTRL_PRESSED)),
    injector.INPUT_RECORD(injector.KEY_EVENT, 0,
                         injector.KEY_EVENT_RECORD(0, 1, VK_V, 0, "\0", injector.LEFT_CTRL_PRESSED)),
]
print(injector.send_records(pid, recs))
print("done")
