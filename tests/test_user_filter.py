# -*- coding: utf-8 -*-
"""「只看我」模式：全文件扫描用户提示词，不受尾部 128KB 窗口限制。"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from server import transcripts
from server.app import TOKEN, app

client = TestClient(app)


def _user(text, ts="2026-10-18T01:00:00.000Z"):
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"content": text}},
                      ensure_ascii=False) + "\n"


def _assistant(text, ts="2026-10-18T01:00:01.000Z"):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"content": [{"type": "text", "text": text}]}},
                      ensure_ascii=False) + "\n"


def _tool_use(ts="2026-10-18T01:00:02.000Z"):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"content": [{"type": "tool_use",
                                                "name": "Bash",
                                                "input": {"command": "ls"}}]}},
                      ensure_ascii=False) + "\n"


def _tool_result(ts="2026-10-18T01:00:03.000Z"):
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"content": [{"type": "tool_result",
                                                "content": "ok"}]}},
                      ensure_ascii=False) + "\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    transcripts._user_events_cache.clear()
    yield tmp_path
    transcripts._user_events_cache.clear()


def _mk(root, slug="E--1workspace", name="s.jsonl", lines=(), mtime=1000):
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("".join(lines), encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_user_mode_returns_only_user_text(_isolate):
    _mk(_isolate, lines=[
        _user("第一条提示词"), _assistant("回答一"),
        _tool_use(), _tool_result(), _user("第二条提示词"), _assistant("回答二"),
    ])
    r = transcripts.read_user_events_dir("E--1workspace", "s.jsonl")
    assert r["found"] is True
    assert [e["text"] for e in r["events"]] == ["第一条提示词", "第二条提示词"]
    assert all(e["role"] == "user" and e["kind"] == "text" for e in r["events"])


def test_user_mode_scans_beyond_tail_window(_isolate, monkeypatch):
    """普通镜像只读尾部窗口，老提示词会被裁掉；user 模式必须全量。"""
    monkeypatch.setattr(transcripts, "TAIL_BYTES", 512)
    lines = [_user("很早的提示词"), _assistant("旧回答")]
    # 尾部塞满无关内容，把早期用户消息挤出 512B 窗口
    lines += [_assistant("填充" * 200), _user("最近的提示词")]
    p = _mk(_isolate, lines=lines)

    tail = transcripts.read_events_dir("E--1workspace", "s.jsonl", 0)
    assert not any("很早的提示词" in e.get("text", "") for e in tail["events"])

    full = transcripts.read_user_events_dir("E--1workspace", "s.jsonl")
    texts = [e["text"] for e in full["events"]]
    assert texts == ["很早的提示词", "最近的提示词"]
    assert full["size"] == p.stat().st_size


def test_user_mode_cache_invalidates_on_write(_isolate):
    p = _mk(_isolate, lines=[_user("甲")])
    r1 = transcripts.read_user_events_dir("E--1workspace", "s.jsonl")
    assert len(r1["events"]) == 1
    with p.open("a", encoding="utf-8") as f:
        f.write(_user("乙"))
    os.utime(p, (2000, 2000))
    r2 = transcripts.read_user_events_dir("E--1workspace", "s.jsonl")
    assert [e["text"] for e in r2["events"]] == ["甲", "乙"]


def test_user_mode_blocks_traversal(_isolate):
    _mk(_isolate)
    for bad in ("..", "../..", "a/b", "a\\b", "", r"E:\1workspace"):
        assert transcripts.read_user_events_dir(bad, "s.jsonl")["found"] is False
    assert transcripts.read_user_events_dir("E--1workspace", "../s.jsonl")["found"] is False
    assert transcripts.read_user_events_dir("E--no-such", "s.jsonl")["found"] is False


def test_api_history_transcript_user_mode(_isolate):
    _mk(_isolate, lines=[_user("我的问题"), _assistant("AI 回答")])
    r = client.get(f"/api/history/transcript?token={TOKEN}"
                   f"&dir=E--1workspace&file=s.jsonl&mode=user")
    assert r.status_code == 200
    texts = [e["text"] for e in r.json()["events"]]
    assert texts == ["我的问题"]
    # mode=user 同样需要鉴权
    assert client.get("/api/history/transcript"
                      "?dir=E--1workspace&file=s.jsonl&mode=user").status_code == 401
