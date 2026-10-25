# -*- coding: utf-8 -*-
"""广播：目标枚举 / PTY 载荷 / 逐目标发送+落库 / API 端点。"""
import pytest
from fastapi.testclient import TestClient

from server import anchors, broadcast, injector, promptlog
from server.app import TOKEN, app

client = TestClient(app)
AUTH = {"X-Auth-Token": TOKEN}


@pytest.fixture(autouse=True)
def _clear_anchors():
    anchors.store.clear()
    yield
    anchors.store.clear()

EXTERNAL = [
    {"pid": 101, "title": "甲会话", "cwd": r"E:\a", "managed": False},
    {"pid": 202, "title": "乙会话", "cwd": r"E:\b", "managed": True},   # 托管里的 claude.exe
]
MANAGED = [{"id": 1, "pid": 909, "title": "", "cwd": r"E:\c"}]


# ---------- 目标枚举 ----------

def test_enumerate_skips_managed_marked_external():
    targets = broadcast.enumerate_targets(EXTERNAL, MANAGED)
    kinds = [(t["kind"], t["pid"] if t["kind"] == "ext" else t["id"])
             for t in targets]
    assert kinds == [("ext", 101), ("mgr", 1)]  # 202 被跳过：只能从 PTY 通道发


# ---------- PTY 载荷 ----------

def test_pty_payload_single_line():
    assert broadcast.build_pty_payload("hello", True) == "hello\r"
    assert broadcast.build_pty_payload("hello", False) == "hello"


def test_pty_payload_multiline_bracketed_paste():
    payload = broadcast.build_pty_payload("第一行\r\n第二行", True)
    assert payload == "\x1b[200~第一行\n第二行\x1b[201~\r"


# ---------- run()：逐目标发送 + 落库 ----------

@pytest.fixture()
def logged(monkeypatch):
    rows = []
    monkeypatch.setattr(promptlog, "log",
                        lambda *a, **kw: rows.append((a, kw)))
    return rows


def test_run_per_target_results_and_logging(monkeypatch, logged):
    sent = {}

    def _send(pid, text, submit):
        sent[pid] = (text, submit)
        return {"ok": True}
    monkeypatch.setattr(injector, "send_text", _send)

    class _Sess:
        def __init__(self, alive=True):
            self._alive = alive
            self.writes = []

        def alive(self):
            return self._alive

        def write(self, data):
            self.writes.append(data)

    sess = _Sess()
    results = broadcast.run("继续", True, EXTERNAL, MANAGED,
                            lambda sid: sess, batch_id="b1")
    assert [r["ok"] for r in results] == [True, True]
    assert sent[101] == ("继续", True)
    assert sess.writes == ["继续\r"]
    # 每个目标一行日志，batch_id 相同
    assert len(logged) == 2
    assert logged[0][0][:3] == ("broadcast", "pid 101", "继续")
    assert logged[0][1]["batch_id"] == "b1" and logged[0][1]["ok"] is True
    assert logged[1][0][1] == "mgr 1"
    assert logged[1][1]["target_title"] == "" and logged[1][1]["cwd"] == r"E:\c"


def test_run_registers_anchor_per_successful_target(monkeypatch, logged):
    """成功注入的目标必须登记锚点（外部 ext / 托管 mgr），供 phase-0 实证认领。"""
    monkeypatch.setattr(injector, "send_text",
                        lambda pid, text, submit: {"ok": True})

    class _Sess:
        def alive(self):
            return True

        def write(self, data):
            pass

    broadcast.run("继续", True, EXTERNAL, MANAGED, lambda sid: _Sess(),
                  batch_id="b-a")
    ext_anchors = anchors.store.recent(r"E:\a")
    mgr_anchors = anchors.store.recent(r"E:\c")
    assert len(ext_anchors) == 1
    assert ext_anchors[0].key == ("ext", 101) and ext_anchors[0].text == "继续"
    assert len(mgr_anchors) == 1
    assert mgr_anchors[0].key == ("mgr", 1)
    assert anchors.store.recent(r"E:\b") == []  # 202 被跳过：不登记


def test_run_failed_target_registers_no_anchor(monkeypatch, logged):
    def _send(pid, text, submit):
        return {"ok": False, "error": "AttachConsole 失败 (err=5)"}
    monkeypatch.setattr(injector, "send_text", _send)
    monkeypatch.setattr(broadcast.injector, "send_text", _send)

    class _Sess:
        def alive(self):
            return True

        def write(self, data):
            pass

    broadcast.run("x", True, EXTERNAL, MANAGED, lambda sid: _Sess(),
                  batch_id="b-f")
    # 101 失败不登记；909 托管成功仍登记
    assert anchors.store.recent(r"E:\a") == []
    assert [a.key for a in anchors.store.recent(r"E:\c")] == [("mgr", 1)]


def test_run_single_failure_does_not_block_others(monkeypatch, logged):
    def _send(pid, text, submit):
        return {"ok": False, "error": "AttachConsole 失败 (err=5)"}
    monkeypatch.setattr(injector, "send_text", _send)
    monkeypatch.setattr(broadcast.injector, "send_text", _send)

    class _Sess:
        def alive(self):
            return True

        def write(self, data):
            pass

    results = broadcast.run("x", True, EXTERNAL, MANAGED,
                            lambda sid: _Sess(), batch_id="b2")
    assert results[0]["ok"] is False and "err=5" in results[0]["error"]
    assert results[1]["ok"] is True
    assert logged[0][1]["ok"] is False and "err=5" in logged[0][1]["error"]


def test_run_dead_managed_reported(monkeypatch, logged):
    results = broadcast.run("x", True, [], MANAGED,
                            lambda sid: None, batch_id="b3")
    assert results[0]["ok"] is False and "已退出" in results[0]["error"]


# ---------- API 端点 ----------

@pytest.fixture()
def patched_sessions(monkeypatch):
    monkeypatch.setattr("server.app.sessions.list_sessions",
                        lambda roots, ttl=0: list(EXTERNAL))
    monkeypatch.setattr("server.app.pty_manager.list",
                        lambda: list(MANAGED))
    monkeypatch.setattr("server.app.pty_manager.root_pids", lambda: {909})


def test_broadcast_requires_auth(patched_sessions):
    r = client.post("/api/broadcast", json={"text": "hi"})
    assert r.status_code == 401


def test_broadcast_rejects_empty(patched_sessions):
    r = client.post("/api/broadcast", json={"text": "  "}, headers=AUTH)
    assert r.status_code == 400


def test_broadcast_dry_run_only_enumerates(patched_sessions, monkeypatch):
    called = []
    monkeypatch.setattr("server.app.broadcast.run",
                        lambda *a, **kw: called.append(1))
    r = client.post("/api/broadcast", json={"text": "hi", "dry_run": True},
                    headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["dry_run"] is True and len(data["targets"]) == 2
    assert called == []  # dry_run 绝不注入


def test_broadcast_real_run_returns_counts(patched_sessions, monkeypatch,
                                           logged):
    monkeypatch.setattr(injector, "send_text",
                        lambda pid, text, submit: {"ok": True})

    class _Sess:
        def alive(self):
            return True

        def write(self, data):
            pass

    monkeypatch.setattr("server.app.pty_manager.get", lambda sid: _Sess())
    r = client.post("/api/broadcast", json={"text": "汇报进度"}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["ok_count"] == 2 and data["total"] == 2 and data["batch_id"]
    assert len(logged) == 2


# ---------- 单发落库 ----------

def test_single_input_logged(monkeypatch, logged):
    monkeypatch.setattr("server.app.sessions.session_by_pid",
                        lambda pid: {"pid": pid, "cwd": r"E:\x", "title": "T"})
    monkeypatch.setattr("server.app.injector.send_text",
                        lambda pid, text, submit: {"ok": True, "events": 5})
    r = client.post("/api/session/555/input",
                    json={"text": "单独这条"}, headers=AUTH)
    assert r.status_code == 200
    assert len(logged) == 1
    args, kw = logged[0]
    assert args[:3] == ("single", "pid 555", "单独这条")
    assert kw["ok"] is True and kw["cwd"] == r"E:\x"


def test_single_input_404_not_logged(monkeypatch, logged):
    monkeypatch.setattr("server.app.sessions.session_by_pid", lambda pid: None)
    r = client.post("/api/session/999/input",
                    json={"text": "x"}, headers=AUTH)
    assert r.status_code == 404
    assert logged == []
