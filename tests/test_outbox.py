# -*- coding: utf-8 -*-
"""发送回执：prompt_log 行过滤 / 受理判定（一对一消耗）/ 内存锚点降级。"""
from datetime import datetime, timedelta, timezone

import pytest

from server import anchors, outbox, promptlog

UTC = timezone.utc
T0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def _log_row(text, ts, ok=True, error=None, cwd=r"E:\proj"):
    return {"ts": ts, "prompt": text, "ok": ok, "error": error, "cwd": cwd}


def _ev(text, ts):
    return {"role": "user", "kind": "text", "text": text,
            "ts": ts.isoformat()}


@pytest.fixture(autouse=True)
def _clean_anchors():
    anchors.store.clear()
    yield
    anchors.store.clear()


@pytest.fixture
def no_mysql(monkeypatch):
    """prompt_log 不可用 → collect 走内存锚点降级。"""
    monkeypatch.setattr(promptlog, "recent_for_target",
                        lambda *a, **k: None)


@pytest.fixture
def log_rows(monkeypatch):
    """可控的 prompt_log 数据源：tests 往盒子里放行。"""
    box = {"rows": []}
    monkeypatch.setattr(promptlog, "recent_for_target",
                        lambda target, hours=36.0: list(box["rows"]))
    return box


@pytest.fixture
def events(monkeypatch):
    """可控的 jsonl 用户事件源。"""
    box = {"events": []}
    monkeypatch.setattr(outbox.transcripts, "read_user_events",
                        lambda cwd, file: {"events": list(box["events"]),
                                           "size": 0, "found": True})
    return box


# ---------- prompt_log 行过滤（pid 复用防护） ----------

def test_collect_filters_stale_pid_reuse(log_rows, events):
    started = T0
    log_rows["rows"] = [
        _log_row("旧进程的消息", T0 - timedelta(hours=5)),       # 早于启动 → 剔除
        _log_row("别的目录的消息", T0 + timedelta(minutes=1),
                 cwd=r"E:\other"),                               # cwd 不符 → 剔除
        _log_row("本次会话的消息", T0 + timedelta(minutes=2)),
    ]
    r = outbox.collect(("ext", 1234), r"E:\proj", None, started.isoformat())
    assert [i["text"] for i in r["items"]] == ["本次会话的消息"]
    assert r["persistent"] is True


def test_collect_without_started_keeps_cwd_match_only(log_rows, events):
    log_rows["rows"] = [
        _log_row("别的目录", T0, cwd=r"E:\other"),
        _log_row("本目录", T0 + timedelta(minutes=1)),
    ]
    r = outbox.collect(("ext", 1234), r"E:\proj", None, None)
    assert [i["text"] for i in r["items"]] == ["本目录"]


# ---------- 受理判定 ----------

def test_pending_when_no_matching_event(log_rows, events):
    log_rows["rows"] = [_log_row("继续优化", T0)]
    r = outbox.collect(("ext", 1), r"E:\proj", "a.jsonl", None)
    assert r["items"][0]["status"] == "pending"
    assert r["items"][0]["ack_ts"] is None


def test_acked_when_event_lands_after_inject(log_rows, events):
    log_rows["rows"] = [_log_row("继续优化", T0)]
    # 忙时排队：事件 40 分钟后才落盘也算受理（无 20s 窗限制）
    events["events"] = [_ev("继续优化", T0 + timedelta(minutes=40))]
    r = outbox.collect(("ext", 1), r"E:\proj", "a.jsonl", None)
    assert r["items"][0]["status"] == "acked"
    assert r["items"][0]["ack_ts"] == (T0 + timedelta(minutes=40)).isoformat()


def test_event_before_inject_does_not_ack(log_rows, events):
    log_rows["rows"] = [_log_row("继续优化", T0)]
    events["events"] = [_ev("继续优化", T0 - timedelta(minutes=3))]
    r = outbox.collect(("ext", 1), r"E:\proj", "a.jsonl", None)
    assert r["items"][0]["status"] == "pending"


def test_failed_row_needs_no_event(log_rows, events):
    log_rows["rows"] = [_log_row("你好", T0, ok=False, error="CONIN$ 打开失败")]
    r = outbox.collect(("ext", 1), r"E:\proj", "a.jsonl", None)
    assert r["items"][0]["status"] == "failed"
    assert r["items"][0]["error"] == "CONIN$ 打开失败"


def test_identical_prompts_consume_events_one_by_one(log_rows, events):
    """连发两条「继续」：一条落盘事件只能确认其中一条（先来先得）。"""
    log_rows["rows"] = [
        _log_row("继续", T0),
        _log_row("继续", T0 + timedelta(minutes=1)),
    ]
    events["events"] = [_ev("继续", T0 + timedelta(minutes=5))]
    r = outbox.collect(("ext", 1), r"E:\proj", "a.jsonl", None)
    assert [i["status"] for i in r["items"]] == ["acked", "pending"]
    # 两条事件都在 → 都受理
    events["events"].append(_ev("继续", T0 + timedelta(minutes=6)))
    r = outbox.collect(("ext", 1), r"E:\proj", "a.jsonl", None)
    assert [i["status"] for i in r["items"]] == ["acked", "acked"]


def test_no_file_means_all_pending(log_rows, events):
    """会话尚未关联 jsonl：诚实给「待受理」，不借别的文件判定。"""
    log_rows["rows"] = [_log_row("继续优化", T0)]
    r = outbox.collect(("ext", 1), r"E:\proj", None, None)
    assert r["items"][0]["status"] == "pending"


# ---------- MySQL 不可用 → 内存锚点降级 ----------

def test_anchor_fallback_when_mysql_down(no_mysql, events):
    # 锚点有 15 分钟 TTL：必须用当前时间，固定历史时刻会被 recent() 过期剪掉
    now = datetime.now(UTC)
    anchors.store.add(("ext", 7), r"E:\proj", "锚点消息", ts=now)
    anchors.store.add(("ext", 8), r"E:\proj", "别的会话", ts=now)
    anchors.store.add(("ext", 7), r"E:\other", "别的目录", ts=now)
    events["events"] = [_ev("锚点消息", now + timedelta(seconds=30))]
    r = outbox.collect(("ext", 7), r"E:\proj", "a.jsonl", None)
    assert r["persistent"] is False
    assert [i["text"] for i in r["items"]] == ["锚点消息"]
    assert r["items"][0]["status"] == "acked"


# ---------- mgr 目标 ----------

def test_mgr_target_label(log_rows, events):
    log_rows["rows"] = [_log_row("托管消息", T0)]
    events["events"] = [_ev("托管消息", T0 + timedelta(seconds=2))]
    r = outbox.collect(("mgr", 3), r"E:\proj", "a.jsonl", None)
    assert r["items"][0]["status"] == "acked"
    # 断言查询用的是 "mgr 3" 标签
    assert outbox._target_label(("mgr", 3)) == "mgr 3"
    assert outbox._target_label(("ext", 9)) == "pid 9"


# ---------- promptlog.recent_for_target SQL ----------

def test_recent_for_target_sql(monkeypatch):
    from tests.test_promptlog import CFG, FakePymysql
    fake = FakePymysql()
    naive = datetime(2026, 10, 25, 10, 0, 0)
    fake.rows = [
        (naive, "消息A", 1, None, r"E:\proj"),
        (naive, "消息B", 0, "AttachConsole 失败", r"E:\proj"),
    ]
    store = promptlog.MySQLPromptLog(CFG, pymysql_mod=fake)
    rows = store.recent_for_target("pid 42", hours=36)
    select, params = [x for x in fake.executed if "FROM prompt_log" in x[0]][0]
    assert "target = %s" in select and "INTERVAL %s HOUR" in select
    assert params == ("pid 42", 36)
    assert len(rows) == 2
    assert rows[0]["ok"] is True and rows[0]["ts"].tzinfo is not None
    assert rows[1]["ok"] is False and rows[1]["error"] == "AttachConsole 失败"


def test_recent_for_target_module_degrades(monkeypatch, tmp_path):
    promptlog.reset_store()
    monkeypatch.setattr(promptlog, "_CONFIG_PATH", tmp_path / "none.json")
    assert promptlog.recent_for_target("pid 1") is None
    promptlog.reset_store()
