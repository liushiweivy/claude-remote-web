# -*- coding: utf-8 -*-
"""纯逻辑单测：会话识别 / 输入记录构造 / transcript 解析 / API 鉴权。"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from server import injector, ptyman, sessions, transcripts
from server.app import TOKEN, app


# ---------- sessions ----------

def test_is_claude_native_exe():
    assert sessions.is_claude(
        "claude.exe",
        r"C:\Users\4v\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
        '"...claude-code\\bin\\claude.exe"') is True


def test_is_claude_node_cli():
    cmd = r'"C:\Program Files\nodejs\node.exe" C:\n\node_modules\@anthropic-ai\claude-code\cli.js'
    assert sessions.is_claude("node.exe", r"C:\Program Files\nodejs\node.exe", cmd) is True


def test_not_claude():
    assert sessions.is_claude("node.exe", r"C:\node\node.exe", "node server.js") is False
    assert sessions.is_claude("python.exe", r"C:\Python312\python.exe", "python") is False


def test_never_match_our_server():
    assert sessions.is_claude(
        "node.exe",
        r"E:\1workspace\claude-remote-web\node_modules\x\claude.exe",
        "claude-remote-web") is False


# ---------- injector ----------

def test_build_send_records_single_line():
    recs = injector.build_send_records("abc", submit=True)
    # 3 字符 × (down+up) + Enter ×2 = 8
    assert len(recs) == 8
    assert all(r.EventType == injector.KEY_EVENT for r in recs)
    assert recs[0].KeyEvent.bKeyDown == 1
    assert recs[0].KeyEvent.UnicodeChar == "a"
    # 抬起事件不带字符（否则 libuv 会把非 ASCII 字符读两遍）
    assert recs[-1].KeyEvent.UnicodeChar == "\0"
    assert recs[-1].KeyEvent.wVirtualKeyCode == injector.VK_RETURN
    assert recs[-1].KeyEvent.bKeyDown == 0
    assert all(recs[i].KeyEvent.UnicodeChar == "\0" for i in range(1, len(recs), 2))


def test_build_send_records_multiline_uses_bracketed_paste():
    recs = injector.build_send_records("a\nb", submit=True)
    seq = "".join(r.KeyEvent.UnicodeChar for r in recs if r.KeyEvent.bKeyDown)
    assert seq == "\x1b[200~a\nb\x1b[201~\r"


def test_build_send_records_crlf_normalized():
    recs = injector.build_send_records("a\r\nb", submit=False)
    seq = "".join(r.KeyEvent.UnicodeChar for r in recs if r.KeyEvent.bKeyDown)
    assert seq == "\x1b[200~a\nb\x1b[201~"  # CRLF 已归一，不自动提交


def test_unicode_chars_in_records():
    recs = injector.build_send_records("中文Ω", submit=False)
    down = [r.KeyEvent.UnicodeChar for r in recs if r.KeyEvent.bKeyDown]
    assert down == ["中", "文", "Ω"]


def test_control_keys():
    esc = injector.control_key("escape")
    assert esc[0].KeyEvent.UnicodeChar == "\x1b"
    assert esc[0].KeyEvent.wVirtualKeyCode == injector.VK_ESCAPE
    cc = injector.control_key("ctrl_c")
    assert cc[0].KeyEvent.UnicodeChar == "\x03"
    assert cc[0].KeyEvent.dwControlKeyState == injector.LEFT_CTRL_PRESSED
    with pytest.raises(ValueError):
        injector.control_key("nope")


def test_clean_env_strips_child_markers(monkeypatch):
    monkeypatch.setattr(os, "environ", {
        "PATH": r"C:\x", "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ID": "uuid", "CLAUDE_PID": "123",
        "CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\cc", "AI_AGENT": "claude-code_x",
        "KEEP": "1"})
    block = ptyman.clean_env()
    pairs = dict(p.split("=", 1) for p in block.split("\0") if p)
    assert "CLAUDE_CODE_CHILD_SESSION" not in pairs
    assert "CLAUDE_CODE_SESSION_ID" not in pairs
    assert "CLAUDE_PID" not in pairs
    assert pairs["PATH"] == r"C:\x" and pairs["KEEP"] == "1"
    assert pairs["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] == "1"


# ---------- transcripts ----------

def test_encode_cwd():
    assert transcripts.encode_cwd(r"C:\Users\4v") == "C--Users-4v"
    assert transcripts.encode_cwd("E:/1workspace/x") == "E--1workspace-x"


def test_parse_line_user_string():
    ev = transcripts.parse_line(json.dumps({
        "type": "user", "timestamp": "2026-10-15T01:00:00.000Z",
        "message": {"content": "你好"}}))
    assert ev["role"] == "user" and ev["text"] == "你好"


def test_parse_line_assistant_text_and_tool():
    line = json.dumps({"type": "assistant", "timestamp": "t", "message": {"content": [
        {"type": "text", "text": "答案"}]}})
    assert transcripts.parse_line(line)["kind"] == "text"
    tool = transcripts.parse_line(json.dumps({"type": "assistant", "timestamp": "t",
        "message": {"content": [{"type": "tool_use", "name": "Bash",
                                  "input": {"command": "ls -la\nsecond"}}]}}))
    assert tool["kind"] == "tool" and tool["name"] == "Bash" and tool["detail"] == "ls -la"
    write_t = transcripts.parse_line(json.dumps({"type": "assistant", "timestamp": "t",
        "message": {"content": [{"type": "tool_use", "name": "Write",
                                  "input": {"file_path": r"E:\x\a.py"}}]}}))
    assert write_t["detail"] == r"E:\x\a.py"


def test_parse_line_skips_meta_sidechain_thinking():
    assert transcripts.parse_line(json.dumps({"type": "summary"})) is None
    assert transcripts.parse_line("not-json") is None
    assert transcripts.parse_line(json.dumps({
        "type": "assistant", "isSidechain": True,
        "message": {"content": [{"type": "text", "text": "x"}]}})) is None
    assert transcripts.parse_line(json.dumps({
        "type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "x"}]}})) is None


def test_parse_line_tool_result_is_separate_kind():
    """工具结果不得伪装成用户提示词：前端要把用户原话高亮成主角，
    工具结果只在折叠的'工具过程'组里出现。"""
    ev = transcripts.parse_line(json.dumps({"type": "user", "timestamp": "t", "message":
        {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}}))
    assert ev["kind"] == "toolresult" and ev["role"] == "user" and ev["is_error"] is True
    assert "工具报错" in ev["text"] and "boom" in ev["text"]
    ok = transcripts.parse_line(json.dumps({"type": "user", "timestamp": "t", "message":
        {"content": [{"type": "tool_result", "content": "done"}]}}))
    assert ok["kind"] == "toolresult" and ok["is_error"] is False


def test_parse_line_null_text_block_does_not_crash():
    """jsonl 是外部数据边界：text:null / 非字符串块不得让整文件解析 500。"""
    ev = transcripts.parse_line(json.dumps({"type": "user", "timestamp": "t", "message":
        {"content": [{"type": "text", "text": None},
                     {"type": "text", "text": "真消息"}]}}))
    assert ev and ev["text"] == "真消息"
    ev2 = transcripts.parse_line(json.dumps({"type": "assistant", "timestamp": "t", "message":
        {"content": [{"type": "text", "text": 123},
                      {"type": "text", "text": "真回复"}]}}))
    assert ev2 and ev2["text"] == "真回复"


def test_parse_line_real_user_text_not_mixed_with_tool_result():
    """同条消息里真有用户文本时，只返回用户提示词事件，绝不拼入工具结果噪音。"""
    ev = transcripts.parse_line(json.dumps({"type": "user", "timestamp": "t", "message":
        {"content": [
            {"type": "text", "text": "这是我的问题"},
            {"type": "tool_result", "content": "noise"}]}}))
    assert ev["kind"] == "text" and ev["text"] == "这是我的问题"
    assert "工具结果" not in ev["text"]


def test_read_events_tail_and_increment(tmp_path, monkeypatch):
    proj = tmp_path / "C--Users-4v"
    proj.mkdir()
    f = proj / "sess.jsonl"

    def line(role, text, ts):
        return json.dumps({"type": role, "timestamp": ts,
                           "message": {"content": text}}, ensure_ascii=False)

    with f.open("w", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(line("user", f"msg{i}", f"2026-10-15T01:00:{i:02d}.000Z") + "\n")
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    r1 = transcripts.read_events(r"C:\Users\4v", "sess.jsonl", after=0)
    assert len(r1["events"]) == 10 and r1["size"] > 0
    offset = r1["size"]
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-10-15T01:01:00.000Z",
                             "message": {"content": [{"type": "text", "text": "追加"}]}},
                            ensure_ascii=False) + "\n")
    r2 = transcripts.read_events(r"C:\Users\4v", "sess.jsonl", after=offset)
    assert len(r2["events"]) == 1 and r2["events"][0]["text"] == "追加"
    # 截断（换文件）后从头读
    f.write_text(line("user", "新", "2026-10-15T02:00:00.000Z") + "\n", encoding="utf-8")
    r3 = transcripts.read_events(r"C:\Users\4v", "sess.jsonl", after=99999)
    assert r3["size"] < 99999 and len(r3["events"]) == 1


def test_huge_last_line_extends_tail_window(tmp_path, monkeypatch):
    """15MB 活跃文件最后一行可能是 >128KB 未写完的巨型 JSON：尾部窗口没有
    换行时必须扩大窗口，否则首屏误报'这条会话还没有消息记录'。"""
    proj = tmp_path / "C--Users-4v"
    proj.mkdir()
    f = proj / "big.jsonl"
    good = json.dumps({"type": "user", "timestamp": "2026-10-15T01:00:00.000Z",
                       "message": {"content": "看得见我"}}, ensure_ascii=False) + "\n"
    with f.open("w", encoding="utf-8") as fh:
        fh.write(good)
        fh.write('{"type": "assistant", "timestamp": "2026-10-15T01:00:01.000Z", '
                 '"message": {"content": [{"type": "text", "text": "' + "x" * 200_000
                 + '"}]}}')  # 无结尾换行 = 正在写入的半截行
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    r = transcripts.read_events(r"C:\Users\4v", "big.jsonl", after=0)
    assert any("看得见我" in e["text"] for e in r["events"])


def test_transcript_file_param_blocks_path_traversal(tmp_path, monkeypatch):
    """file 参数来自网络：..\\.. / 绝对路径 / 非 jsonl 名一律拒绝（服务绑定
    0.0.0.0 且可能经公网暴露，不能让持 token 者读任意文件）。"""
    proj = tmp_path / "C--Users-4v"
    proj.mkdir()
    (proj / "ok.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-10-15T01:00:00.000Z",
                    "message": {"content": "ok"}}) + "\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("password", encoding="utf-8")
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    assert transcripts.read_events(r"C:\Users\4v", "ok.jsonl", 0)["found"] is True
    for bad in ("../../secret.txt", "..\\..\\secret.txt",
                str(secret).replace("\\", "\\\\"), "ok.txt", "a/b.jsonl", ""):
        assert transcripts.read_events(r"C:\Users\4v", bad, 0)["found"] is False


def test_partial_last_line_not_consumed(tmp_path, monkeypatch):
    proj = tmp_path / "C--x"
    proj.mkdir()
    f = proj / "s.jsonl"
    full = json.dumps({"type": "user", "message": {"content": "ok"}}) + "\n"
    f.write_bytes(full.encode() + b'{"type":"user",')  # 半个 JSON
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    r = transcripts.read_events(r"C:\x", "s.jsonl", 0)
    assert len(r["events"]) == 1
    assert r["size"] == len(full.encode())  # 残缺行留在原地


# ---------- API ----------

client = TestClient(app)


def test_health_anonymous():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["authed"] is False


def test_auth_required():
    assert client.get("/api/sessions").status_code == 401
    assert client.get(f"/api/sessions?token={TOKEN}").status_code == 200


def test_session_list_shape():
    r = client.get(f"/api/sessions?token={TOKEN}")
    data = r.json()
    assert isinstance(data["sessions"], list) and isinstance(data["managed"], list)
    for s in data["sessions"]:
        assert {"pid", "cwd", "managed", "started"} <= set(s)


def test_managed_bad_dir_rejected():
    r = client.post(f"/api/managed?token={TOKEN}", json={"cwd": r"Z:\no\such\dir"})
    assert r.status_code == 400


def test_index_served():
    assert client.get("/").status_code == 200
