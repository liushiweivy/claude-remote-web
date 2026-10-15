# -*- coding: utf-8 -*-
"""在独立控制台窗口启动一个干净的 Claude Code（洗掉父 claude 会话标记），
首条消息以 argv 传入（Unicode 命令行，不经控制台输入通道，故无 CJK 叠字）。

用法:
  python start_cc_window.py ["首条消息"] [工作目录]
默认: 消息=你好，工作目录=E:\\1workspace
注意: 不要用含中文的 .bat 做这件事——cmd 按 GBK 解析 UTF-8 批处理文件会错位。
"""
import os
import subprocess
import sys

# 与 server/ptyman.py 的 STRIP_ENV 保持一致
STRIP_ENV = (
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "AI_AGENT",
)

CREATE_NEW_CONSOLE = 0x00000010


def main() -> None:
    msg = sys.argv[1] if len(sys.argv) > 1 else "你好"
    workdir = sys.argv[2] if len(sys.argv) > 2 else r"E:\1workspace"

    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"

    proc = subprocess.Popen(
        ["cmd.exe", "/k", "claude", msg],
        cwd=workdir,
        env=env,
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )
    print(f"STARTED cmd_pid={proc.pid} cwd={workdir} msg={msg}")


if __name__ == "__main__":
    main()
