# -*- coding: utf-8 -*-
"""托管会话：由本网页用 ConPTY(pywinpty) 拉起的 claude，浏览器 xterm 全双工。"""
from __future__ import annotations

import itertools
import os
import subprocess
import threading
import time
from datetime import datetime

import winpty

from .replay import ReplayBuffer

CMD = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "cmd.exe")
DEFAULT_COLS, DEFAULT_ROWS = 120, 32

# 本服务若由某个 claude 会话内部启动，子进程会继承这些"我是子会话/管道"标记，
# 导致新 claude 关闭 transcript 落盘。托管会话必须洗掉。
STRIP_ENV = (
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "AI_AGENT",
)


def clean_env() -> str:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    return "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"


class ManagedSession:
    def __init__(self, sid: int, cwd: str):
        self.id = sid
        self.cwd = cwd
        self.pty = winpty.PTY(DEFAULT_COLS, DEFAULT_ROWS)
        ok = self.pty.spawn(CMD, "cmd.exe /c claude", cwd=cwd, env=clean_env())
        if not ok:
            raise RuntimeError("PTY spawn 返回 False")
        self.started = True
        self.started_at = datetime.now().astimezone()
        self._lock = threading.Lock()
        # 回滚缓冲 + 常驻读取线程：没人连 WS 时 PTY 输出也持续落缓冲
        # （旧实现只在 WS 连接期间读，断开期间输出淤积可能撑满 ConPTY 缓冲），
        # 重连后客户端报偏移补差额（见 replay.py / app.py ws_managed）。
        self.replay = ReplayBuffer()
        self._reader = threading.Thread(
            target=self._pump_replay, daemon=True, name=f"crw-pty-reader-{sid}")
        self._reader.start()

    def _pump_replay(self) -> None:
        """把 PTY 输出搬进回滚缓冲，直到会话退出且读干。"""
        while True:
            chunk = self.read()
            if chunk:
                self.replay.append(chunk)
                continue
            if not self.alive():
                # 进程退出后可能还有残留输出，读干再收尾
                tail = self.read()
                if tail:
                    self.replay.append(tail)
                    continue
                self.replay.mark_eof()
                return
            time.sleep(0.03)

    @property
    def pid(self) -> int | None:
        return self.pty.pid

    def alive(self) -> bool:
        try:
            return bool(self.pty.isalive())
        except winpty.WinptyError:
            return False

    def read(self) -> str:
        try:
            with self._lock:
                return self.pty.read(blocking=False) or ""
        except winpty.WinptyError:
            return ""

    def write(self, data: str) -> int:
        with self._lock:
            return self.pty.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if cols > 0 and rows > 0:
            with self._lock:
                self.pty.set_size(cols, rows)

    def stop(self) -> None:
        pid = self.pid
        if pid:  # 收掉 cmd -> claude 整棵树（按根 PID，绝不按镜像名）；PTY 对象随 GC 释放
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)


class PtyManager:
    def __init__(self):
        self._sessions: dict[int, ManagedSession] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, cwd: str) -> ManagedSession:
        with self._lock:
            sid = next(self._ids)
            sess = ManagedSession(sid, cwd)
            self._sessions[sid] = sess
            return sess

    def get(self, sid: int) -> ManagedSession | None:
        return self._sessions.get(sid)

    def root_pids(self) -> set[int]:
        return {s.pid for s in self._sessions.values() if s.pid}

    def list(self) -> list[dict]:
        dead = []
        out = []
        for s in self._sessions.values():
            if not s.alive():
                dead.append(s.id)
                continue
            out.append({"id": s.id, "pid": s.pid, "cwd": s.cwd,
                        "started": s.started_at.isoformat()})
        for sid in dead:
            self._sessions.pop(sid, None)
        return out

    def stop(self, sid: int) -> bool:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s:
            s.stop()
            return True
        return False
