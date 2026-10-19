# -*- coding: utf-8 -*-
"""E2E 用：以干净环境（无子会话标记）在新控制台窗口启动 claude。"""
import os
import subprocess
import sys

STRIP_ENV = (
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "AI_AGENT",
)

cwd = sys.argv[1] if len(sys.argv) > 1 else r"E:\1workspace\claude-remote-web"
env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
CREATE_NEW_CONSOLE = 0x10
p = subprocess.Popen(["cmd", "/k", "claude"], cwd=cwd, env=env,
                     creationflags=CREATE_NEW_CONSOLE)
print("launched cmd pid:", p.pid)
