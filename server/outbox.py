# -*- coding: utf-8 -*-
"""发送回执：从会话台发出的每条消息，到底被 Claude 受理了没有。

痛点（2026-10-25）：Claude 忙时注入的文本在 TUI 里排队，可能几分钟~几小时
后才落盘成 jsonl 用户事件；「已注入 ✓」只证明按键进了控制台，第二天用户
完全无法查证那条消息发出去没有。

数据源：
- prompt_log（MySQL，持久）：跨天回执的事实源；target = "pid N" / "mgr N"
- anchors.store（内存，降级）：MySQL 未配置/故障时用，只含成功注入且有时效

受理判定：注入时刻之后（含时钟噪声宽限）在关联 jsonl 里出现文本一致的
用户事件 = 已受理。事件一对一消耗：连发两条相同文本不会被同一条事件
同时确认。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import anchors, promptlog, transcripts

OUTBOX_HOURS = 36.0          # 回执回看窗口：覆盖「昨晚发的今早来查」
CLOCK_SKEW_SEC = 5.0         # 落盘事件允许比注入时刻早的时钟噪声
PID_REUSE_GRACE_SEC = 120.0  # 注入行早于会话启动超过此值 = pid 复用的陈旧行


def _target_label(key: tuple) -> str:
    return f"pid {key[1]}" if key[0] == "ext" else f"mgr {key[1]}"


def _parse_started(started) -> datetime | None:
    if not started:
        return None
    try:
        dt = datetime.fromisoformat(str(started))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rows_from_log(key: tuple, cwd: str | None,
                   started: datetime | None, hours: float) -> list[dict] | None:
    """prompt_log 行 → 统一行；MySQL 不可用返回 None（降级信号）。

    pid 会被操作系统复用：只收 cwd 一致且不早于本次会话启动的行。
    """
    raw = promptlog.recent_for_target(_target_label(key), hours)
    if raw is None:
        return None
    rows = []
    for r in raw:
        if started is not None and r["ts"] is not None and \
                r["ts"] < started - timedelta(seconds=PID_REUSE_GRACE_SEC):
            continue
        if cwd and r["cwd"] and r["cwd"] != cwd:
            continue
        rows.append({"ts": r["ts"], "text": r["prompt"],
                     "ok": r["ok"], "error": r["error"]})
    return rows


def _rows_from_anchors(key: tuple, cwd: str | None) -> list[dict]:
    """降级：内存锚点（含冷启动回放）只覆盖成功注入，没有失败记录。"""
    return [{"ts": a.ts, "text": a.text, "ok": True, "error": None}
            for a in anchors.store.recent(cwd or "") if a.key == key]


def _ack_match(rows: list[dict], events: list[dict]) -> None:
    """就地标注 rows[i]["ack_ts"]：事件按时间升序一对一贪心消耗。"""
    parsed = []
    for ev in events:
        raw_ts = ev.get("ts")
        if not raw_ts:
            continue
        try:
            ets = transcripts._parse_ts(raw_ts)
        except ValueError:
            continue
        parsed.append((ets, ev.get("text") or ""))
    parsed.sort(key=lambda x: x[0])
    used = [False] * len(parsed)
    for row in rows:  # rows 已按 ts 升序
        if not row["ok"] or row["ts"] is None:
            continue
        lo = row["ts"] - timedelta(seconds=CLOCK_SKEW_SEC)
        for i, (ets, text) in enumerate(parsed):
            if used[i] or ets < lo:
                continue
            if anchors.text_matches(row["text"], text):
                used[i] = True
                row["ack_ts"] = ets
                break


def collect(key: tuple, cwd: str | None, file: str | None, started,
            hours: float = OUTBOX_HOURS) -> dict:
    """汇总某会话的发送回执。

    key: ("ext", pid) | ("mgr", id)；file: 当前关联的 jsonl 文件名（无则全部
    只能给「待受理」，不借别的文件判定）。返回：
      {"items": [{"ts","text","status","ack_ts","error"}],  # ts 升序
       "persistent": bool}   # False = MySQL 不可用，仅内存锚点（近期）
    status: acked（已受理）| pending（已注入待受理）| failed（注入失败）
    """
    started_dt = _parse_started(started)
    rows = _rows_from_log(key, cwd, started_dt, hours)
    persistent = rows is not None
    if rows is None:
        rows = _rows_from_anchors(key, cwd)
    rows.sort(key=lambda r: r["ts"] or datetime.min.replace(tzinfo=timezone.utc))

    if file:
        events = transcripts.read_user_events(cwd, file)["events"]
        _ack_match(rows, events)

    items = [{
        "ts": r["ts"].isoformat() if r["ts"] else None,
        "text": r["text"],
        "status": "failed" if not r["ok"]
                  else ("acked" if r.get("ack_ts") else "pending"),
        "ack_ts": r["ack_ts"].isoformat() if r.get("ack_ts") else None,
        "error": r["error"],
    } for r in rows]
    return {"items": items, "persistent": persistent}
