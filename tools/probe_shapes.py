# -*- coding: utf-8 -*-
"""在真实 Claude TUI 上二分 CJK 事件形状，寻找不重复的注入方式。"""
import sys
import time

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server import injector
from server.injector import INPUT_RECORD, KEY_EVENT, KEY_EVENT_RECORD

pid = int(sys.argv[1])


def ev(ch, down, vk=0, scan=0, ctrl=0):
    return INPUT_RECORD(KEY_EVENT, 0, KEY_EVENT_RECORD(1 if down else 0, 1, vk, scan, ch, ctrl))


def send(name, recs, settle=0.5):
    injector.send_records(pid, recs)
    time.sleep(settle)
    print("sent", name)


# VK_PROCESSKEY=0xE5：IME 已处理
send("E5_downup", [ev("壬", 1, 0xE5), ev("壬", 0, 0xE5)])
# 仅 down, VK_PROCESSKEY
send("E5_down", [ev("癸", 1, 0xE5)])
# 带扫描码
send("scan_downup", [ev("子", 1, 0, 0x20), ev("子", 0, 0, 0x20)])
# 只发抬起带字符
send("up_only", [ev("丑", 0)])
# VK_PACKET down only + 普通 up
send("packet_down_plainup", [ev("寅", 1, 0xE7), ev("\0", 0)])
