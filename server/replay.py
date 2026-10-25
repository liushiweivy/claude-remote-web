# -*- coding: utf-8 -*-
"""托管终端输出回滚缓冲：WS 断连期间 PTY 输出不丢，重连按偏移补差额。

借鉴 claudecodeui 的 seq+subscribe：客户端重连时报「已收到 N 字符」，
服务端只补 N 之后的内容；缓冲已覆盖（N 太旧）则整段重发、客户端重置终端。

偏移按字符计（WS 文本帧两端口径一致）。纯逻辑类，与 PTY/网络无关，可单测。
"""
from __future__ import annotations

import threading

DEFAULT_CAP = 262_144  # 256K 字符：手机 4G/隧道重连下载约 0.25MB，可接受


class ReplayBuffer:
    """定长回滚缓冲：追加文本，按全局偏移切片。

    - ``total``：历史上写入的总字符数（单调递增）
    - ``oldest``：当前缓冲里最早字符的全局偏移（< oldest 的内容已被挤掉）
    """

    def __init__(self, cap: int = DEFAULT_CAP):
        if cap <= 0:
            raise ValueError("cap 必须为正")
        self.cap = cap
        self._buf = ""
        self.total = 0
        self.oldest = 0
        self.eof = False  # PTY 退出且读干后由读取线程置 True
        self.cond = threading.Condition()

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        with self.cond:
            self._buf += chunk
            self.total += len(chunk)
            if len(self._buf) > self.cap:
                drop = len(self._buf) - self.cap
                self._buf = self._buf[drop:]
                self.oldest += drop
            self.cond.notify_all()

    def slice_from(self, offset: int) -> str | None:
        """取 offset 之后（含）的全部缓冲内容；offset 不可得返回 None。

        offset > total（客户端来自更新的服务端进程）或 offset < oldest
        （断连太久缓冲已翻篇）都是缺口，调用方应整段重发。
        """
        with self.cond:
            if offset < self.oldest or offset > self.total:
                return None
            return self._buf[offset - self.oldest:]

    def mark_eof(self) -> None:
        """PTY 退出且读干：唤醒所有等待者收尾。"""
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def wait_from(self, offset: int, timeout: float = 30.0) -> dict:
        """阻塞等到有 offset 之后的新数据（或超时/EOF），供 WS 泵线程调用。

        返回 {"chunk", "total", "gap", "eof"}；gap=True 表示 offset 不可得。
        子类/调用方负责设置 self.eof（PTY 退出且读干后置 True）。
        """
        with self.cond:
            if offset < self.oldest or offset > self.total:
                return {"chunk": None, "total": self.total, "gap": True,
                        "eof": self.eof, "oldest": self.oldest}
            while self.total <= offset and not self.eof:
                self.cond.wait(timeout)
                if self.total <= offset and not self.eof:
                    break  # 超时返回空 chunk，让调用方有机会检查 WS 活性
            return {"chunk": self._buf[offset - self.oldest:],
                    "total": self.total, "gap": False, "eof": self.eof}
