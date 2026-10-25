# -*- coding: utf-8 -*-
"""CC 官方会话登记 ~/.claude/sessions/<pid>.json 读取与覆盖解析测试。

该登记是 pid→sessionId 的 ground truth（2.1.283+），app 层在时间/锚点解析
之后用它覆盖结果；读不到登记或 jsonl 尚未落盘时必须保留旧答案，绝不返回错文件。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from server import session_registry

UTC = timezone.utc

SID1 = "de1c7c2f-8acc-4e4a-9981-37e25707975b"
SID2 = "fb195c6d-6dbe-498d-bc7a-5554130d7e18"
SID_BAD = "not-a-uuid"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(session_registry, "SESSIONS_DIR", tmp_path)
    session_registry._file_cache.clear()
    session_registry._scan_cache.update(ts=0.0, items={})
    yield tmp_path
    session_registry._file_cache.clear()
    session_registry._scan_cache.update(ts=0.0, items={})


def _write(d, pid: int, sid: str = SID1, cwd: str = r"E:\1workspace",
           updated: int = 1, **extra):
    data = {"pid": pid, "sessionId": sid, "cwd": cwd,
            "updatedAt": updated, **extra}
    (d / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_entries_parses_valid_and_skips_garbage(_isolated):
    """合法登记按 pid 收录；pid 不符/JSON 坏/sid 非法/非数字 pid 全跳过。"""
    d = _isolated
    _write(d, 100, SID1)
    (d / "200.json").write_text(
        json.dumps({"pid": 999, "sessionId": SID2, "cwd": r"E:\x"}),
        encoding="utf-8")  # 文件名 pid 与正文 pid 不符
    (d / "201.json").write_text("{broken json", encoding="utf-8")
    _write(d, 202, SID_BAD)  # sessionId 形态非法
    (d / "abc.json").write_text(
        json.dumps({"pid": 300, "sessionId": SID2, "cwd": r"E:\x"}),
        encoding="utf-8")
    items = session_registry.entries(ttl=0)
    assert set(items) == {100}
    assert items[100]["sessionId"] == SID1


def test_for_pid_miss_returns_none(_isolated):
    assert session_registry.for_pid(404, ttl=0) is None


def test_scan_ttl_and_signature_cache(_isolated):
    """扫描 TTL 内目录不重扫（改写也看不到）；ttl=0 重扫，单文件签名变化才重读。"""
    import os
    d = _isolated
    f100 = d / "100.json"
    _write(d, 100, SID1, updated=1)
    first = session_registry.entries(ttl=1000)
    assert first[100]["updatedAt"] == 1
    _write(d, 100, SID2, updated=2)
    # 两个 UUID 等长、连续写入可能落在同一个 mtime tick（签名不变）；
    # 生产中 /clear 改写相隔数秒，mtime 必变。测试显式拉开 mtime 保证确定性。
    os.utime(f100, ns=(f100.stat().st_atime_ns, f100.stat().st_mtime_ns + 10_000))
    # TTL 内：仍是目录缓存
    assert session_registry.entries(ttl=1000)[100]["sessionId"] == SID1
    # 绕过目录 TTL：文件签名变了（mtime/size），重读拿新 sessionId
    items = session_registry.entries(ttl=0)
    assert items[100]["sessionId"] == SID2 and items[100]["updatedAt"] == 2


def test_deleted_entry_drops_from_cache(_isolated):
    d = _isolated
    _write(d, 100, SID1)
    assert 100 in session_registry.entries(ttl=0)
    (d / "100.json").unlink()
    assert session_registry.entries(ttl=0) == {}
    assert 100 not in session_registry._file_cache


def test_resolve_entry_hits_jsonl(_isolated, monkeypatch):
    entry = _write(_isolated, 100, SID1)
    monkeypatch.setattr(
        session_registry.transcripts, "list_files_cached",
        lambda cwd, ttl=None: [
            {"file": "other.jsonl", "title": "别的"},
            {"file": f"{SID1}.jsonl", "title": "本会话"}])
    hit = session_registry.resolve_entry(entry)
    assert hit == {"file": f"{SID1}.jsonl", "title": "本会话"}


def test_resolve_entry_missing_jsonl_returns_none(_isolated, monkeypatch):
    """登记的 sessionId 尚无落盘文件（/clear 后首条事件前）→ None，
    调用方据此保留时间匹配的旧答案。"""
    entry = _write(_isolated, 100, SID1)
    monkeypatch.setattr(
        session_registry.transcripts, "list_files_cached",
        lambda cwd, ttl=None: [{"file": "other.jsonl", "title": "别的"}])
    assert session_registry.resolve_entry(entry) is None


@pytest.mark.parametrize("mut", [
    lambda e: e.pop("sessionId"),
    lambda e: e.pop("cwd"),
])
def test_resolve_entry_requires_sid_and_cwd(_isolated, mut):
    entry = _write(_isolated, 100, SID1)
    mut(entry)
    assert session_registry.resolve_entry(entry) is None


def test_for_roots_walks_parent_chain_picks_latest(_isolated, monkeypatch):
    """托管 PTY：登记 pid 是 claude.exe，要沿父链归到 root（cmd.exe）；
    同一 root 下多条登记取 updatedAt 最新；不在 root 树下的排除。"""
    _write(_isolated, 101, SID1, updated=10)   # 101 → 201 → root 555
    _write(_isolated, 102, SID2, updated=20)   # 102 → 202 → root 555（更新）
    _write(_isolated, 103, "aaaabbbb-cccc-dddd-eeee-ffffffffffff", updated=99)
    # 103 → 303 → 304（链上没有 root）
    rows = [
        (101, 201, "claude.exe"), (201, 555, "cmd.exe"),
        (102, 202, "claude.exe"), (202, 555, "cmd.exe"),
        (103, 303, "claude.exe"), (303, 304, "cmd.exe"),
        (555, 1, "cmd.exe"),
    ]
    monkeypatch.setattr(session_registry.winproc, "snapshot", lambda: rows)
    out = session_registry.for_roots({555: r"E:\1workspace", 777: r"E:\x"}, ttl=0)
    assert set(out) == {555}          # 无登记的 root 不出现
    assert out[555]["pid"] == 102     # 同 root、同深度取最近更新


def test_for_roots_chain_cycle_is_safe(_isolated, monkeypatch):
    """父链异常成环不得死循环。"""
    _write(_isolated, 101, SID1)
    rows = [(101, 201, "claude.exe"), (201, 202, "cmd.exe"),
            (202, 201, "cmd.exe")]
    monkeypatch.setattr(session_registry.winproc, "snapshot", lambda: rows)
    assert session_registry.for_roots({555: r"E:\1workspace"}, ttl=0) == {}


# ---------- app 层覆盖语义（resolve_files 之后再覆盖） ----------

def _live_session(pid: int = 21308, cwd: str = r"E:\1workspace"):
    """登记/进程出生时间一致的活会话（供 entry_is_live 通过）。"""
    t = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
    return (
        {"pid": pid, "managed": False, "cwd": cwd, "started": t.isoformat()},
        {"sessionId": SID1, "pid": pid, "cwd": cwd,
         "startedAt": int(t.timestamp() * 1000)},
    )


def test_apply_registry_overrides_time_resolution(monkeypatch):
    from server import app
    sess, entry = _live_session()
    external = [sess]
    resolved = {("ext", 21308): {"file": "wrong.jsonl", "title": "错配"}}
    monkeypatch.setattr(app.session_registry, "for_pid",
                        lambda pid, ttl=None: entry)
    monkeypatch.setattr(app.session_registry, "resolve_entry",
                        lambda e: {"file": f"{SID1}.jsonl", "title": "本会话"})
    app._apply_registry(external, [], resolved)
    assert resolved[("ext", 21308)]["file"] == f"{SID1}.jsonl"


def test_apply_registry_keeps_time_answer_when_jsonl_absent(monkeypatch):
    from server import app
    sess, entry = _live_session()
    external = [sess]
    resolved = {("ext", 21308): {"file": "timeguess.jsonl", "title": "时间匹配"}}
    monkeypatch.setattr(app.session_registry, "for_pid",
                        lambda pid, ttl=None: entry)
    monkeypatch.setattr(app.session_registry, "resolve_entry", lambda e: None)
    app._apply_registry(external, [], resolved)
    assert resolved[("ext", 21308)]["file"] == "timeguess.jsonl"


def test_apply_registry_managed_via_roots(monkeypatch):
    from server import app
    managed = [{"id": 3, "pid": 555}]
    resolved = {("mgr", 3): {"file": None, "title": None}}
    monkeypatch.setattr(app.session_registry, "for_roots",
                        lambda roots, ttl=None: {555: {"pid": 102}})
    monkeypatch.setattr(app.session_registry, "resolve_entry",
                        lambda e: {"file": f"{SID2}.jsonl", "title": "托管"})
    app._apply_registry([], managed, resolved)
    assert resolved[("mgr", 3)]["file"] == f"{SID2}.jsonl"


# ---------- HIGH 修复：陈旧登记（pid 复用）不得覆盖正确解析 ----------

def test_entry_is_live_cwd_and_start_match():
    t = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
    ticks = int(t.timestamp() * 1e7) + session_registry.FILETIME_EPOCH_TICKS
    entry = {"pid": 100, "cwd": r"E:\1workspace",
             "startedAt": int(t.timestamp() * 1000),
             "procStart": str(ticks)}
    assert session_registry.entry_is_live(
        entry, r"e:\1workspace", t.isoformat()) is True  # 盘符大小写不敏感


def test_entry_is_live_procstart_microsecond_exact():
    t = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
    ticks = int(t.timestamp() * 1e7) + session_registry.FILETIME_EPOCH_TICKS
    entry = {"pid": 100, "cwd": r"E:\1workspace",
             "startedAt": int((t.timestamp() + 3.3) * 1000),  # startedAt 晚 3.3s
             "procStart": str(ticks)}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", t.isoformat()) is True


def test_entry_is_live_rejects_cwd_mismatch():
    t = datetime(2026, 10, 10, tzinfo=UTC)
    entry = {"pid": 100, "cwd": r"E:\other",
             "startedAt": int(t.timestamp() * 1000)}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", t.isoformat()) is False


def test_entry_is_live_rejects_stale_pid_reuse():
    """旧进程 taskkill 后登记残留，pid 被新进程复用：
    登记 startedAt（几天前）与现进程出生时间相差悬殊 → 拒绝覆盖。"""
    old = datetime(2026, 10, 1, tzinfo=UTC)
    now = datetime(2026, 10, 21, tzinfo=UTC)
    entry = {"pid": 100, "cwd": r"E:\1workspace",
             "startedAt": int(old.timestamp() * 1000)}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", now.isoformat()) is False


def test_entry_is_live_rejects_stale_procstart():
    """主路径：真实残留登记带着旧进程的 procStart（FILETIME），
    与新进程出生相差 20 天 → 2s 容差外，拒绝覆盖。"""
    old = datetime(2026, 10, 1, tzinfo=UTC)
    now = datetime(2026, 10, 21, tzinfo=UTC)
    old_ticks = int(old.timestamp() * 1e7) + session_registry.FILETIME_EPOCH_TICKS
    entry = {"pid": 100, "cwd": r"E:\1workspace",
             "procStart": str(old_ticks),
             "startedAt": int(old.timestamp() * 1000)}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", now.isoformat()) is False


def test_entry_is_live_startedat_allows_observed_3s_skew():
    """实测 startedAt（CC 会话初始化）比 OS 进程出生晚 2.3~3.6s。"""
    birth = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
    entry = {"pid": 100, "cwd": r"E:\1workspace",
             "startedAt": int((birth.timestamp() + 3.6) * 1000)}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", birth.isoformat()) is True


def test_entry_is_live_without_started_iso_checks_cwd_only():
    """winproc 读不到出生时间（started=None）时无法判陈旧，退化为只验 cwd。"""
    entry = {"pid": 100, "cwd": r"E:\1workspace", "startedAt": 1}
    assert session_registry.entry_is_live(entry, r"E:\1workspace", None) is True
    assert session_registry.entry_is_live(entry, r"E:\x", None) is False


def test_entry_is_live_unparseable_times_fails_closed():
    """有出生时间可比对、但登记时间字段全坏：宁可不覆盖（fail-closed）。"""
    birth = datetime(2026, 10, 21, tzinfo=UTC)
    entry = {"pid": 100, "cwd": r"E:\1workspace"}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", birth.isoformat()) is False


def test_entry_is_live_bad_procstart_falls_back_to_startedat_tolerance():
    """procStart 字段坏掉、startedAt 晚 3.6s：必须按 startedAt 的 10s 容差放行，
    不能因 procStart 存在就套 2s 容差误杀活进程。"""
    birth = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
    entry = {"pid": 100, "cwd": r"E:\1workspace",
             "procStart": "not-a-number",
             "startedAt": int((birth.timestamp() + 3.6) * 1000)}
    assert session_registry.entry_is_live(
        entry, r"E:\1workspace", birth.isoformat()) is True


def test_apply_registry_rejects_stale_entry(monkeypatch):
    """旧会话残留登记 + pid 复用：不得覆盖锚点/时间匹配的正确答案。"""
    from server import app
    old_ms = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
    now_iso = datetime(2026, 10, 21, tzinfo=UTC).isoformat()
    external = [{"pid": 21308, "managed": False,
                 "cwd": r"E:\1workspace", "started": now_iso}]
    resolved = {("ext", 21308): {"file": "anchor-won.jsonl", "title": "锚点"}}
    monkeypatch.setattr(app.session_registry, "for_pid",
                        lambda pid, ttl=None: {"pid": pid, "cwd": r"E:\1workspace",
                                               "startedAt": old_ms,
                                               "sessionId": SID1})
    monkeypatch.setattr(app.session_registry, "resolve_entry",
                        lambda e: {"file": "stale.jsonl", "title": "陈旧"})
    app._apply_registry(external, [], resolved)
    assert resolved[("ext", 21308)]["file"] == "anchor-won.jsonl"


def test_resolve_ext_pid_rejects_stale_entry(monkeypatch):
    """2.5s 轮询热路径同样要防陈旧登记覆盖。"""
    from server import app
    old_ms = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
    now_iso = datetime(2026, 10, 21, tzinfo=UTC).isoformat()
    sess = {"pid": 21308, "managed": False,
            "cwd": r"E:\1workspace", "started": now_iso}
    monkeypatch.setattr(app.sessions, "list_sessions",
                        lambda roots, ttl=None: [sess])
    monkeypatch.setattr(app.pty_manager, "list", lambda: [])
    monkeypatch.setattr(app.transcripts, "resolve_files",
                        lambda group, anchors=None:
                        {("ext", 21308): {"file": "anchor-won.jsonl"}})
    monkeypatch.setattr(app.session_registry, "for_pid",
                        lambda pid, ttl=None: {"pid": pid, "cwd": r"E:\1workspace",
                                               "startedAt": old_ms,
                                               "sessionId": SID1})
    monkeypatch.setattr(app.session_registry, "resolve_entry",
                        lambda e: {"file": "stale.jsonl", "title": "陈旧"})
    _sess, r = app.resolve_ext_pid(21308)
    assert r["file"] == "anchor-won.jsonl"


# ---------- MEDIUM 修复：for_roots cwd 过滤 + 最浅祖先优先 ----------

def test_for_roots_prefers_shallow_same_cwd_over_nested_newer(_isolated, monkeypatch):
    """root 下两层 claude（外层托管本体 depth 小，内层 Bash 起的 claude 更新）：
    必须取更浅的外层；cwd 不符的登记直接排除，即使更新。"""
    _write(_isolated, 101, SID1, r"E:\1workspace", updated=10)   # depth 2
    _write(_isolated, 105, SID2, r"E:\1workspace", updated=99)   # depth 3
    _write(_isolated, 106, "ccccdddd-eeee-ffff-0000-111111111111",
           r"E:\other", updated=100)                             # cwd 不符
    rows = [
        (101, 201, "claude.exe"), (201, 555, "cmd.exe"),
        (105, 205, "claude.exe"), (205, 204, "cmd.exe"),
        (204, 555, "cmd.exe"),
        (106, 206, "claude.exe"), (206, 204, "cmd.exe"),
        (555, 1, "cmd.exe"),
    ]
    monkeypatch.setattr(session_registry.winproc, "snapshot", lambda: rows)
    out = session_registry.for_roots({555: r"E:\1workspace"}, ttl=0)
    assert set(out) == {555}
    assert out[555]["pid"] == 101
    assert out[555]["sessionId"] == SID1
