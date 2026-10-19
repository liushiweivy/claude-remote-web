# -*- coding: utf-8 -*-
"""对比 CJK 注入变体：VK_PACKET / 仅down / 重复计数，看屏幕回显。"""
import ctypes as C
import os
import sys
import time

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server import injector
from server.injector import INPUT_RECORD, KEY_EVENT, KEY_EVENT_RECORD

pid = int(sys.argv[1])
VK_PACKET = 0xE7


def rec(ch, down, vk=0, repeat=1):
    return INPUT_RECORD(KEY_EVENT, 0, KEY_EVENT_RECORD(1 if down else 0, repeat, vk, 0, ch, 0))


variants = {
    "A_packet": [rec("甲", 1, VK_PACKET), rec("甲", 0, VK_PACKET)],
    "B_downonly": [rec("乙", 1)],
    "C_repeat2": [rec("丙", 1, repeat=2)],
    "D_packet_noup": [rec("丁", 1, VK_PACKET)],
}
for name, recs in variants.items():
    injector.send_records(pid, recs)
    time.sleep(0.4)
print("sent all")
