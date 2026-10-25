# -*- coding: utf-8 -*-
"""注入锚点：服务端精确知道「T 时刻把文本 X 注入给了会话 K（cwd D）」。

落盘用户事件在几十~几百毫秒后带着一致文本出现在某个 jsonl 里 —— 这是
时间启发式给不了的实证。transcripts.resolve_files 用锚点做 phase-0 硬认领，
修掉「同 cwd 多个 claude 几乎同时启动 / 老进程内新开会话」时纯时间匹配
张冠李戴的串台问题（2026-10-21 二代串台）。

- 内存表，线程安全；每条锚点按自身注入时间 + ttl 过期（默认 15 分钟）
- 冷启动从 prompt_log 回放近 2h 成功注入（REPLAY_WINDOW_SEC，行内时间计）
- 文本比较：空白归一化后比较前 PREFIX_LEN 字；CONIN$ 中文叠字走 squeeze 兜底
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ANCHOR_TTL_SEC = 900.0          # 实时锚点存活 15 分钟（实测落盘延迟 < 0.5s）
REPLAY_WINDOW_SEC = 2 * 3600.0  # 冷启动回放最近 2h 的成功注入
REPLAY_GRACE_SEC = 600.0        # 回放行的过期宽限：窗口边缘行不至于刚启动即剪
MATCH_WINDOW_SEC = 20.0         # 落盘事件与注入时刻允许的时间差
PREFIX_LEN = 16                 # 只比前 16 个归一化字符，容忍 TUI 换行/截断差异
MAX_ANCHORS = 512               # 容量兜底，防异常情况下内存无界增长

_WS_RE = re.compile(r"\s+")
_DUP_RUN_RE = re.compile(r"(.)\1+")
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(text: str) -> str:
    """压缩所有连续空白为单空格并去首尾（jsonl 文本与注入文本的空白对齐）。"""
    return _WS_RE.sub(" ", text or "").strip()


def _squeeze(text: str) -> str:
    """折叠连续重复字符：'继继续续' -> '继续'（CONIN$ 中文叠字兜底）。"""
    return _DUP_RUN_RE.sub(r"\1", text)


def text_matches(anchor_text: str, event_text: str, n: int = PREFIX_LEN) -> bool:
    """归一化前缀匹配；直接前缀不符时对双方做叠字折叠再比一次。

    折叠会把正常的叠词（'哈哈'）也压成单字，只作为 20s 时间窗 + 排他配对
    之内的兜底证据，不单独使用。
    """
    a, b = normalize(anchor_text), normalize(event_text)
    if not a or not b:
        return False
    k = min(n, len(a))
    if b[:k] == a[:k]:
        return True
    sa, sb = _squeeze(a).replace(" ", ""), _squeeze(b).replace(" ", "")
    k2 = min(n, len(sa))
    return bool(sa) and sb[:k2] == sa[:k2]


def clean_payload(data: str) -> str:
    """托管 PTY 通道收到的可能是 bracketed-paste 包裹 + 结尾回车：剥掉后
    才是 jsonl 用户事件里的提示词文本。"""
    text = (data or "").replace(_PASTE_START, "").replace(_PASTE_END, "")
    return text.strip("\r\n").strip()


@dataclass(frozen=True)
class Anchor:
    key: tuple          # ("ext", pid) | ("mgr", id)
    cwd: str
    ts: datetime        # 注入时刻，aware UTC
    text: str
    expires_at: datetime


class AnchorStore:
    """线程安全的锚点内存表。ttl 以每条锚点的 ts 为起点，回放行用自己的 ttl。"""

    def __init__(self, default_ttl: float = ANCHOR_TTL_SEC):
        self._lock = threading.RLock()
        self._items: list[Anchor] = []
        self._default_ttl = default_ttl

    def add(self, key: tuple, cwd: str | None, text: str,
            ts: datetime | None = None, ttl: float | None = None) -> None:
        ts = ts or _now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        ttl = self._default_ttl if ttl is None else ttl
        anchor = Anchor(key=key, cwd=cwd or "", ts=ts, text=text or "",
                        expires_at=ts + timedelta(seconds=ttl))
        with self._lock:
            self._items.append(anchor)
            if len(self._items) > MAX_ANCHORS:
                # 挤掉最老的条目（add 基本按时间递增；排序保证确定性）
                self._items.sort(key=lambda a: a.ts)
                del self._items[:len(self._items) - MAX_ANCHORS]

    def replay(self, rows, window_sec: float = REPLAY_WINDOW_SEC,
               grace_sec: float = REPLAY_GRACE_SEC) -> int:
        """冷启动回放 prompt_log 近 2h 成功注入。

        rows: [{"kind": "ext"|"mgr", "id": int, "cwd": str,
                "prompt": str, "ts": aware datetime}]
        过期按行内注入时刻计（window + 启动宽限），不按启动时刻重置。
        """
        n = 0
        for r in rows:
            try:
                kind = r.get("kind")
                rid = r.get("id")
                ts = r.get("ts")
                prompt = r.get("prompt") or ""
                if kind not in ("ext", "mgr") or rid is None or ts is None:
                    continue
                self.add((kind, int(rid)), r.get("cwd") or "", prompt,
                         ts=ts, ttl=window_sec + grace_sec)
                n += 1
            except (TypeError, ValueError):
                continue
        return n

    def prune(self, now: datetime | None = None) -> None:
        now = now or _now()
        with self._lock:
            self._items = [a for a in self._items if a.expires_at > now]

    def recent(self, cwd: str | None, now: datetime | None = None) -> list[Anchor]:
        """返回该 cwd 未过期锚点，按注入时间升序（配对时时间差决胜更直观）。"""
        now = now or _now()
        cwd = cwd or ""
        with self._lock:
            live = [a for a in self._items
                    if a.cwd == cwd and a.expires_at > now]
        live.sort(key=lambda a: a.ts)
        return live

    def clear(self) -> None:
        with self._lock:
            self._items = []


# 全局单例：app / broadcast 发送点登记，resolve_files 读取
store = AnchorStore()
