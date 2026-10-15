# -*- coding: utf-8 -*-
"""Raw 模式靶子：模拟 Ink/claude.exe 直接读按键记录。"""
import msvcrt
f = open(r"E:\1workspace\claude-remote-web\data\raw.txt", "w", encoding="utf-8")
while True:
    ch = msvcrt.getwch()
    f.write(repr(ch) + "\n")
    f.flush()
    if ch == "\x03":
        break
