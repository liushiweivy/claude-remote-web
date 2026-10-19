# -*- coding: utf-8 -*-
"""托管通道端到端：PtyManager 起真 claude，写中文，回读屏幕验证不重复。"""
import sys
import time

sys.path.insert(0, r"E:\1workspace\claude-remote-web")
from server.ptyman import PtyManager

mgr = PtyManager()
s = mgr.start(r"E:\1workspace\claude-remote-web")
print("managed id:", s.id, "pid:", s.pid)
def drain(sess, tries=40):
    out = []
    for _ in range(tries):
        chunk = sess.read()
        if chunk:
            out.append(chunk)
        else:
            time.sleep(0.1)
    return "".join(out)

time.sleep(12)  # 等 claude 启动
buf = drain(s)
s.write("只回复 ZQ_MGR_OK 这几个字符，不要用工具\r")
time.sleep(14)
buf += drain(s)
tail = buf.replace("\x1b", "")
# 去掉大部分 ANSI 残余后看关键行
import re
tail = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB0]|\x1b[=>]", "", tail)
for line in tail.splitlines()[-25:]:
    if line.strip():
        print(line[:120])
mgr.stop(s.id)
