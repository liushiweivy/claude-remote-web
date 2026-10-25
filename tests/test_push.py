# -*- coding: utf-8 -*-
"""push 模块：尾部判定 / busy→idle 状态机 / 订阅存取 / HTTP 端点。"""
import json
import threading
import time
import unittest.mock as mock

from fastapi.testclient import TestClient

from server import app as app_mod
from server import push


def _assistant_line(stop_reason, text="做完了"):
    return json.dumps({
        "type": "assistant",
        "message": {"stop_reason": stop_reason,
                    "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False)


# ---------- tail_status ----------

def test_tail_done():
    tail = _assistant_line("end_turn", "全部改完了") + "\n"
    assert push.tail_status(tail) == ("done", "全部改完了")


def test_tail_permission():
    tail = _assistant_line("tool_use") + "\n"
    kind, _ = push.tail_status(tail)
    assert kind == "permission"


def test_tail_skips_broken_last_line():
    tail = _assistant_line("end_turn", "收尾") + "\n" + '{"type":"assistant","mes'
    assert push.tail_status(tail) == ("done", "收尾")


def test_tail_ignores_non_assistant_after():
    tail = (_assistant_line("end_turn", "OK") + "\n"
            + json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    assert push.tail_status(tail) == ("done", "OK")


def test_tail_unknown_reason_returns_none():
    assert push.tail_status(_assistant_line("pause_turn") + "\n") is None
    assert push.tail_status("") is None
    assert push.tail_status("not json at all\n") is None


def test_tail_snippet_truncated():
    tail = _assistant_line("end_turn", "长" * 200) + "\n"
    kind, snippet = push.tail_status(tail)
    assert kind == "done" and len(snippet) == 60


# ---------- CompletionTracker ----------

T0 = 1_700_000_000.0  # 用真实纪元量级，避开 last_push=0 的 dedup 初始窗


def test_tracker_busy_to_idle_fires_once():
    tr = push.CompletionTracker(idle_after=45, dedup=180)
    assert tr.tick([("f", T0)], now=T0) == []            # busy
    assert tr.tick([("f", T0)], now=T0 + 20) == []       # 仍 busy（20s<45s）
    assert tr.tick([("f", T0)], now=T0 + 50) == ["f"]    # 跳变 idle
    assert tr.tick([("f", T0)], now=T0 + 300) == []      # 没再写入不重复触发


def test_tracker_dedup():
    tr = push.CompletionTracker(idle_after=45, dedup=180)
    tr.tick([("f", T0)], now=T0)
    assert tr.tick([("f", T0)], now=T0 + 100) == ["f"]      # 第一次 idle
    tr.tick([("f", T0 + 110)], now=T0 + 110)                # 又 busy
    assert tr.tick([("f", T0 + 110)], now=T0 + 200) == []   # 距上次 <180s：拦下
    tr.tick([("f", T0 + 300)], now=T0 + 300)
    assert tr.tick([("f", T0 + 300)], now=T0 + 400) == ["f"]  # 距上次 300s：放行


def test_tracker_idle_first_seen_no_fire():
    tr = push.CompletionTracker()
    assert tr.tick([("f", 0.0)], now=10_000.0) == []  # 首见就闲 ≠ 跳变


# ---------- 订阅存取 ----------

def test_sub_store_roundtrip(tmp_path):
    f = tmp_path / "subs.json"
    sub1 = {"endpoint": "https://push/a", "keys": {"p256dh": "x", "auth": "y"}}
    sub2 = {"endpoint": "https://push/b", "keys": {"p256dh": "x", "auth": "y"}}
    assert push.save_sub(sub1, f) == 1
    assert push.save_sub(sub2, f) == 2
    assert push.save_sub(sub1, f) == 2  # 同 endpoint 覆盖不重复
    assert push.remove_sub("https://push/a", f) == 1
    assert [s["endpoint"] for s in push.load_subs(f)] == ["https://push/b"]


def test_load_subs_corrupt(tmp_path):
    f = tmp_path / "subs.json"
    f.write_text("not json", encoding="utf-8")
    assert push.load_subs(f) == []
    assert push.load_subs(tmp_path / "missing.json") == []


# ---------- VAPID ----------

def test_vapid_created_and_reused(tmp_path):
    f = tmp_path / "vapid.json"
    d = push.load_or_create_vapid(f)
    assert len(d["public"]) == 87          # 未压缩点 urlsafe b64 无填充
    assert "PRIVATE KEY" in d["private"]
    assert push.load_or_create_vapid(f) == d  # 复用不重新生成
    f.write_text("garbage", encoding="utf-8")
    d2 = push.load_or_create_vapid(f)         # 损坏则重建
    assert len(d2["public"]) == 87 and d2["private"] != d["private"]


# ---------- 巡检主循环（注入假睡眠/假发送/假时钟） ----------

def test_watcher_fires_on_busy_to_idle(tmp_path):
    proj = tmp_path / "proj-a"
    proj.mkdir()
    (proj / "s1.jsonl").write_text(
        _assistant_line("end_turn", "任务完成") + "\n", encoding="utf-8")
    subs = tmp_path / "subs.json"
    push.save_sub({"endpoint": "https://push/x",
                   "keys": {"p256dh": "k", "auth": "a"}}, subs)

    sent = []
    stop = threading.Event()
    t0 = time.time()
    tick_now = [t0]
    calls = [0]

    def fake_sleep(_):
        calls[0] += 1
        if calls[0] > 1:      # 第 1 轮保持 busy（now≈mtime），之后每轮快进 60s
            tick_now[0] += 60
        if calls[0] >= 3:
            stop.set()

    with mock.patch("time.time", lambda: tick_now[0]):
        push.watcher_loop(projects_dir=tmp_path, subs_path=subs, poll=0.01,
                          sleep_fn=fake_sleep,
                          send_fn=lambda t, b, tag: sent.append((t, b)),
                          stop=stop)
    assert sent == [("✅ 会话跑完了", "proj-a · 任务完成")]


def test_watcher_no_subs_no_scan(tmp_path):
    """没订阅时巡检不碰盘（mtime 扫描被跳过，状态机从未 tick）。"""
    subs = tmp_path / "subs.json"
    subs.write_text("[]", encoding="utf-8")
    stop = threading.Event()
    rounds = [0]

    def fake_sleep(_):
        rounds[0] += 1
        if rounds[0] >= 2:
            stop.set()

    tracker = push.CompletionTracker()
    push.watcher_loop(projects_dir=tmp_path, subs_path=subs, poll=0.01,
                      tracker=tracker, sleep_fn=fake_sleep,
                      send_fn=lambda *a, **k: None, stop=stop)
    assert tracker._state == {}


# ---------- HTTP 端点 ----------

def _save_to(sub, path):
    subs = [s for s in push.load_subs(path) if s.get("endpoint") != sub.get("endpoint")]
    subs.append(sub)
    path.write_text(json.dumps(subs), encoding="utf-8")
    return len(subs)


def _remove_from(endpoint, path):
    subs = [s for s in push.load_subs(path) if s.get("endpoint") != endpoint]
    path.write_text(json.dumps(subs), encoding="utf-8")
    return len(subs)


def test_push_endpoints_require_token():
    c = TestClient(app_mod.app)
    assert c.get("/api/push/vapid").status_code == 401
    assert c.post("/api/push/subscribe",
                  json={"subscription": {"endpoint": "x", "keys": {}}}).status_code == 401


def test_push_subscribe_flow(tmp_path, monkeypatch):
    subs = tmp_path / "subs.json"
    # 端点默认参数在定义期绑定模块常量，monkeypatch 常量无效，直接换函数
    monkeypatch.setattr(push, "save_sub", lambda sub: _save_to(sub, subs))
    monkeypatch.setattr(push, "remove_sub", lambda ep: _remove_from(ep, subs))
    monkeypatch.setattr(push, "load_or_create_vapid",
                        lambda: {"private": "k", "public": "B" * 87})
    c = TestClient(app_mod.app)
    auth = {"X-Auth-Token": app_mod.TOKEN}

    r = c.get("/api/push/vapid", headers=auth)
    assert r.status_code == 200 and r.json()["publicKey"] == "B" * 87

    sub = {"endpoint": "https://push/test1", "keys": {"p256dh": "k", "auth": "a"}}
    r = c.post("/api/push/subscribe", json={"subscription": sub}, headers=auth)
    assert r.json() == {"ok": True, "count": 1}
    assert json.loads(subs.read_text(encoding="utf-8"))[0]["endpoint"] == sub["endpoint"]

    r = c.post("/api/push/subscribe", json={"subscription": {"keys": {}}}, headers=auth)
    assert r.status_code == 400  # 缺 endpoint

    r = c.post("/api/push/unsubscribe", json={"subscription": sub}, headers=auth)
    assert r.json() == {"ok": True, "count": 0}
