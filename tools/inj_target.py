# -*- coding: utf-8 -*-
"""注入测试靶子：在独立控制台窗口里逐行读 stdin 写文件。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
out = open(r"E:\1workspace\claude-remote-web\data\inj.txt", "a", encoding="utf-8")
print("INJ_TARGET_READY pid=%d" % __import__("os").getpid())
for line in sys.stdin:
    out.write(line)
    out.flush()
