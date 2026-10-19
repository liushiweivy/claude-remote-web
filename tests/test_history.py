# -*- coding: utf-8 -*-
"""历史会话视图：跨项目扫盘、微信桥来源标记、目录参数安全。"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from server import transcripts
from server.app import TOKEN, app


def _user_line(text, ts="2026-10-18T01:00:00.000Z"):
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"content": text}},
                      ensure_ascii=False) + "\n"


def _mk_proj(root, slug, files):
    """files: {文件名: (内容行列表, mtime)}"""
    d = root / slug
    d.mkdir(parents=True)
    for name, (lines, mtime) in files.items():
        p = d / name
        p.write_text("".join(lines), encoding="utf-8")
        os.utime(p, (mtime, mtime))
    return d


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(transcripts, "BRIDGE_SESSIONS_LOG",
                        tmp_path / "no-such" / "sessions.log")
    transcripts._history_cache.update(ts=0.0, fp=(), items=[])
    yield tmp_path
    transcripts._history_cache.update(ts=0.0, fp=(), items=[])


def test_history_spans_projects_sorted_by_mtime(_isolate):
    root = _isolate
    _mk_proj(root, "C--Users-4v", {
        "old.jsonl": ([_user_line("旧会话")], 1000),
    })
    _mk_proj(root, "E--1workspace", {
        "new.jsonl": ([_user_line("新会话")], 3000),
        "mid.jsonl": ([_user_line("中间会话")], 2000),
    })
    items = transcripts.list_history()
    assert [i["file"] for i in items] == ["new.jsonl", "mid.jsonl", "old.jsonl"]
    assert items[0]["dir"] == "E--1workspace"
    assert items[0]["preview"].startswith("新会话")
    assert all(i["bridge"] is False for i in items)


def test_history_limit_takes_most_recent(_isolate):
    root = _isolate
    files = {f"s{i:02d}.jsonl": ([_user_line(f"第{i}条")], 1000 + i)
             for i in range(40)}
    _mk_proj(root, "C--Users-4v", files)
    items = transcripts.list_history(limit=10)
    assert len(items) == 10
    assert items[0]["file"] == "s39.jsonl"


def test_history_marks_bridge_sessions(_isolate, tmp_path, monkeypatch):
    root = _isolate
    sid = "12345678-abcd-1234-abcd-1234567890ab"
    _mk_proj(root, "E--1workspace", {
        f"{sid}.jsonl": ([_user_line("桥任务")], 2000),
        "ffffffff-0000-0000-0000-000000000000.jsonl": ([_user_line("本地")], 3000),
    })
    log = tmp_path / "logs" / "sessions.log"
    log.parent.mkdir()
    log.write_text(
        f"2026-10-18 08:00:00 START cwd=E:\\1workspace resume=- sudo=0 prompt=桥任务\n"
        f"2026-10-18 08:01:00 DONE state=done elapsed=60s session={sid} "
        f"jsonl=C:\\Users\\4v\\.claude\\projects\\E--1workspace\\{sid}.jsonl\n",
        encoding="utf-8")
    monkeypatch.setattr(transcripts, "BRIDGE_SESSIONS_LOG", log)
    items = {i["file"]: i for i in transcripts.list_history()}
    assert items[f"{sid}.jsonl"]["bridge"] is True
    assert items["ffffffff-0000-0000-0000-000000000000.jsonl"]["bridge"] is False


def test_bridge_session_ids_reads_rotated_backups(tmp_path, monkeypatch):
    """Rolling 备份里的旧 session id 也要算（否则老桥任务丢「微信桥」标）。"""
    log = tmp_path / "sessions.log"
    log.write_text("DONE session=aaaaaaaa-1111-1111-1111-111111111111 jsonl=-\n",
                   encoding="utf-8")
    (tmp_path / "sessions.log.1").write_text(
        "DONE session=bbbbbbbb-2222-2222-2222-222222222222 jsonl=-\n",
        encoding="utf-8")
    (tmp_path / "sessions.log.tmp").write_text("不是日志\n", encoding="utf-8")
    monkeypatch.setattr(transcripts, "BRIDGE_SESSIONS_LOG", log)
    ids = transcripts.bridge_session_ids()
    assert "aaaaaaaa-1111-1111-1111-111111111111" in ids
    assert "bbbbbbbb-2222-2222-2222-222222222222" in ids


def test_bridge_session_ids_missing_log_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "BRIDGE_SESSIONS_LOG",
                        tmp_path / "gone" / "sessions.log")
    assert transcripts.bridge_session_ids() == set()


def test_history_cache_invalidates_on_fingerprint_change(_isolate):
    root = _isolate
    d = _mk_proj(root, "C--Users-4v", {"a.jsonl": ([_user_line("甲")], 1000)})
    first = transcripts.list_history(ttl=3600)
    assert [i["file"] for i in first] == ["a.jsonl"]
    # TTL 内但指纹变了（新文件落盘）→ 必须立刻可见，不能等缓存到期
    p = d / "b.jsonl"
    p.write_text(_user_line("乙"), encoding="utf-8")
    os.utime(p, (2000, 2000))
    second = transcripts.list_history(ttl=3600)
    assert [i["file"] for i in second] == ["b.jsonl", "a.jsonl"]


def test_read_events_dir_reads_and_blocks_traversal(_isolate):
    root = _isolate
    _mk_proj(root, "E--1workspace", {"ok.jsonl": ([_user_line("历史内容")], 1000)})
    r = transcripts.read_events_dir("E--1workspace", "ok.jsonl", 0)
    assert r["found"] is True
    assert any("历史内容" in e["text"] for e in r["events"])
    # dir 同样来自网络参数：任何带分隔符/跳出 projects 根的形态都拒
    for bad in ("..", "../..", "a/b", "a\\b", "", r"E:\1workspace"):
        assert transcripts.read_events_dir(bad, "ok.jsonl", 0)["found"] is False
    assert transcripts.read_events_dir("E--1workspace", "../ok.jsonl", 0)["found"] is False
    assert transcripts.read_events_dir("E--no-such", "ok.jsonl", 0)["found"] is False


# ---------- API ----------

client = TestClient(app)


def test_api_history_requires_auth():
    assert client.get("/api/history").status_code == 401
    r = client.get(f"/api/history?token={TOKEN}")
    assert r.status_code == 200 and r.json()["items"] == []


def test_api_history_and_transcript(_isolate):
    _mk_proj(_isolate, "C--Users-4v", {
        "h.jsonl": ([_user_line("接口回看")], 1000),
    })
    r = client.get(f"/api/history?token={TOKEN}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["file"] == "h.jsonl"
    r2 = client.get(f"/api/history/transcript?token={TOKEN}"
                    f"&dir=C--Users-4v&file=h.jsonl")
    assert r2.status_code == 200
    assert any("接口回看" in e["text"] for e in r2.json()["events"])
