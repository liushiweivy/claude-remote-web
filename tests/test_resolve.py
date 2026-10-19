# -*- coding: utf-8 -*-
"""会话进程 ↔ jsonl 文件全局排他解析的回归测试。

复现的现网 bug：同 cwd 多个 claude 进程时，旧实现让多个会话指向同一个
（最近修改的）jsonl —— 标题重复、transcript 串台、发消息后不知道谁收到。
"""
import json
import os
from datetime import datetime, timezone, timedelta

from server import sessions, transcripts

# 测试基准时间（UTC 存储，与 jsonl timestamp 一致）
T0 = datetime(2026, 10, 16, 0, 34, 28, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_jsonl(path, first_ts: datetime | None, *, mtime: datetime | None = None,
                 title: str | None = None, preview: str | None = None,
                 head_meta: bool = False):
    """造一个 jsonl。first_ts=None 模拟通篇无时间戳；head_meta=True 先写若干
    新版无时间戳元数据行（last-prompt/mode/atis-latch）。"""
    lines = []
    if head_meta:
        lines.append(json.dumps({"type": "last-prompt", "leafUuid": "x",
                                 "sessionId": "s"}))
        # 400 行 ≈ 10KB，超过旧实现的 8KB 扫描窗口
        for _ in range(400):
            lines.append(json.dumps({"type": "atis-latch"}))
    if first_ts:
        if title is not None:
            lines.append(json.dumps({"type": "ai-title", "title": title,
                                     "timestamp": _iso(first_ts)}))
        else:
            lines.append(json.dumps(
                {"type": "user", "timestamp": _iso(first_ts),
                 "message": {"content": preview or "第一条消息"}},
                ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mt = mtime or first_ts or T0
    os.utime(path, (mt.timestamp(), mt.timestamp()))


def _procs(*specs):
    return [{"key": ("ext", pid), "cwd": r"C:\Users\4v", "started": _iso(st)}
            for pid, st in specs]


def _setup(tmp_path, monkeypatch, files):
    proj = tmp_path / "C--Users-4v"
    proj.mkdir(exist_ok=True)
    for name, kwargs in files.items():
        _write_jsonl(proj / name, **kwargs)
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    transcripts._file_cache.clear()


def test_two_procs_two_files_each_own(tmp_path, monkeypatch):
    """核心回归：两个进程各有自己的文件，不得串。"""
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0 + timedelta(seconds=12),
                    "title": "微信桥接", "mtime": T0 + timedelta(minutes=20)},
        "b.jsonl": {"first_ts": T0 + timedelta(seconds=60+37),
                    "title": "lol 网页", "mtime": T0 + timedelta(minutes=20)},
    })
    procs = _procs((100, T0), (200, T0 + timedelta(seconds=66)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "a.jsonl"
    assert res[("ext", 100)]["title"] == "微信桥接"
    assert res[("ext", 200)]["file"] == "b.jsonl"
    assert res[("ext", 200)]["title"] == "lol 网页"


def test_idle_proc_must_not_borrow_most_recent(tmp_path, monkeypatch):
    """核心回归：新进程还没产生任何文件时，结果是 None —— 绝不借用最活跃文件。"""
    _setup(tmp_path, monkeypatch, {
        "active.jsonl": {"first_ts": T0, "title": "正在忙的会话",
                         "mtime": T0 + timedelta(minutes=30)},
    })
    # 新进程在 active.jsonl 创建 9 分钟后才启动，自己还没发言
    procs = _procs((100, T0), (300, T0 + timedelta(minutes=9)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "active.jsonl"
    assert res[("ext", 300)]["file"] is None
    assert res[("ext", 300)]["title"] is None


def test_file_older_than_proc_start_not_claimable(tmp_path, monkeypatch):
    """文件首事件比进程启动还早（超出时钟容差）→ 该进程不能认领，
    即使时间差绝对值很小（旧 abs() 算法在此误判）。"""
    ts = T0 + timedelta(minutes=2, seconds=11)
    _setup(tmp_path, monkeypatch, {
        "f.jsonl": {"first_ts": ts, "title": "属于老进程",
                    "mtime": ts + timedelta(minutes=18)},
    })
    # 老进程提前 37s 启动；新进程比文件首事件晚 6s 启动
    procs = _procs((100, ts - timedelta(seconds=37)),
                   (200, ts + timedelta(seconds=6)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "f.jsonl"
    assert res[("ext", 200)]["file"] is None


def test_proc_owns_multiple_files_latest_is_current(tmp_path, monkeypatch):
    """同一进程 /clear 后产生第二个 session：当前文件取 first_ts 最新者。"""
    _setup(tmp_path, monkeypatch, {
        "old.jsonl": {"first_ts": T0 + timedelta(minutes=8),
                      "title": "内网穿透",
                      "mtime": T0 + timedelta(minutes=9)},
        "new.jsonl": {"first_ts": T0 + timedelta(minutes=15),
                      "title": "你好",
                      "mtime": T0 + timedelta(minutes=16)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "new.jsonl"
    assert res[("ext", 100)]["title"] == "你好"


def test_resumed_old_session_claimed_via_mtime(tmp_path, monkeypatch):
    """resume：文件首事件远早于进程启动，但进程启动后文件被续写（mtime 新）。"""
    _setup(tmp_path, monkeypatch, {
        "ancient.jsonl": {"first_ts": T0 - timedelta(days=1),
                          "title": "昨天的会话",
                          "mtime": T0 + timedelta(seconds=40)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "ancient.jsonl"


def test_resume_file_not_written_after_start_stays_unclaimed(tmp_path, monkeypatch):
    """旧文件在进程启动后没再被写 → 不能只因为它'最新'就挂上。"""
    _setup(tmp_path, monkeypatch, {
        "ancient.jsonl": {"first_ts": T0 - timedelta(days=1),
                          "title": "昨天的会话",
                          "mtime": T0 - timedelta(hours=2)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] is None


def test_first_timestamp_behind_timestamp_free_header(tmp_path, monkeypatch):
    """新版 jsonl 头部 80+ 行无 timestamp 元数据，第一条带 ts 的记录在 8KB 之后
    仍要能取到时间（旧实现只读前 8KB → None → 文件被跳过）。"""
    _setup(tmp_path, monkeypatch, {
        "meta.jsonl": {"first_ts": T0 + timedelta(seconds=5),
                       "head_meta": True, "title": "元数据头会话",
                       "mtime": T0 + timedelta(minutes=5)},
    })
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    files = transcripts.list_files(r"C:\Users\4v")
    assert files[0]["first_ts"] is not None
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "meta.jsonl"
    assert res[("ext", 100)]["title"] == "元数据头会话"


def test_two_homeless_procs_one_resumed_file_exclusive(tmp_path, monkeypatch):
    """两个无文件进程 + 一个启动后被续写的旧文件：只能分给其中一个。"""
    _setup(tmp_path, monkeypatch, {
        "r.jsonl": {"first_ts": T0 - timedelta(days=1),
                    "title": "resume 的",
                    "mtime": T0 + timedelta(seconds=30)},
    })
    procs = _procs((100, T0), (200, T0 + timedelta(seconds=10)))
    res = transcripts.resolve_files(procs)
    owners = [k for k, v in res.items() if v["file"] == "r.jsonl"]
    assert len(owners) == 1


def test_groups_by_cwd_are_independent(tmp_path, monkeypatch):
    """不同 cwd 的同名文件互不干扰。"""
    for cwd_enc in ("C--Users-4v", "E--1workspace"):
        (tmp_path / cwd_enc).mkdir(exist_ok=True)
        p = tmp_path / cwd_enc / "f.jsonl"
        _write_jsonl(p, T0 + timedelta(seconds=20), title="T",
                     mtime=T0 + timedelta(minutes=2))
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    procs = [
        {"key": ("ext", 1), "cwd": r"C:\Users\4v", "started": _iso(T0)},
        {"key": ("ext", 2), "cwd": r"E:\1workspace", "started": _iso(T0)},
    ]
    res = transcripts.resolve_files(procs)
    assert res[("ext", 1)]["file"] == "f.jsonl"
    assert res[("ext", 2)]["file"] == "f.jsonl"


def test_cache_invalidated_when_file_changes(tmp_path, monkeypatch):
    """标题缓存不能挡住新文件：目录指纹（name/mtime/size）变化即失效。"""
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0, "title": "A", "mtime": T0},
    })
    procs = _procs((100, T0))
    assert transcripts.resolve_files(procs)[("ext", 100)]["file"] == "a.jsonl"
    # 新会话文件出现（新进程启动 5 分钟后）
    _write_jsonl(tmp_path / "C--Users-4v" / "b.jsonl",
                 T0 + timedelta(minutes=5, seconds=2), title="B",
                 mtime=T0 + timedelta(minutes=6))
    procs = _procs((100, T0), (200, T0 + timedelta(minutes=5)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "a.jsonl"
    assert res[("ext", 200)]["file"] == "b.jsonl"


# ---------- sessions：父链 / 子会话过滤（mock winproc） ----------

def test_managed_marker_walks_full_parent_chain(monkeypatch):
    """托管根在第 6 层（旧实现只查 4 层会漏标 → 同一会话在两区重复出现）。"""
    rows = [(10, 1, "cmd.exe")] + [
        (100, 10, "cmd.exe"), (101, 100, "cmd.exe"),
        (102, 101, "cmd.exe"), (103, 102, "cmd.exe"),
        (104, 103, "cmd.exe"), (105, 104, "claude.exe")]
    monkeypatch.setattr(sessions.winproc, "snapshot", lambda: rows)
    monkeypatch.setattr(sessions.winproc, "peb_info", lambda pid: {
        "cwd": r"C:\x", "image": r"...\@anthropic-ai\claude-code\bin\claude.exe",
        "cmdline": "claude", "env": {}})
    monkeypatch.setattr(sessions.winproc, "process_started",
                        lambda pid: datetime.now(timezone.utc))
    out = sessions.list_sessions({10})
    assert len(out) == 1 and out[0]["pid"] == 105 and out[0]["managed"] is True


def test_child_agent_session_filtered(monkeypatch):
    """Task/Agent 子会话（CLAUDE_CODE_CHILD_SESSION=1）不应出现在列表：
    它非交互、无自己的 transcript，出现必与父会话撞标题。"""
    rows = [(200, 1, "claude.exe"), (201, 1, "claude.exe")]
    env_by_pid = {
        200: {},
        201: {"CLAUDE_CODE_CHILD_SESSION": "1"},
    }
    monkeypatch.setattr(sessions.winproc, "snapshot", lambda: rows)
    monkeypatch.setattr(sessions.winproc, "peb_info", lambda pid: {
        "cwd": r"C:\x", "image": r"...\@anthropic-ai\claude-code\bin\claude.exe",
        "cmdline": "claude", "env": env_by_pid[pid]})
    monkeypatch.setattr(sessions.winproc, "process_started",
                        lambda pid: datetime.now(timezone.utc))
    out = sessions.list_sessions(set())
    assert [s["pid"] for s in out] == [200]


# ---------- 代码审查回归（2026-10-16） ----------

def test_naive_timestamp_does_not_crash_resolve(tmp_path, monkeypatch):
    """HIGH-1：无 timezone 的时间戳（第三方/格式变体）不得让 resolve 抛
    naive/aware 相减的 TypeError —— 单坏文件曾让全部会话 API 500。"""
    proj = tmp_path / "C--Users-4v"
    proj.mkdir()
    bad = proj / "naive.jsonl"
    bad.write_text(json.dumps({"type": "user", "timestamp": "2026-10-16T00:34:40.000",
                               "message": {"content": "无时区时间戳"}}, ensure_ascii=False)
                   + "\n", encoding="utf-8")
    os.utime(bad, (T0.timestamp(), T0.timestamp()))
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)  # 不抛异常即合格
    assert res[("ext", 100)]["file"] == "naive.jsonl"


def test_round2_equal_mtime_tie_no_crash_and_exclusive(tmp_path, monkeypatch):
    """MED-1：两个 resume 文件 mtime 完全相同（微秒级）时排序不得落到 dict
    比较抛 TypeError；文件仍须排他归属。"""
    _setup(tmp_path, monkeypatch, {
        "r1.jsonl": {"first_ts": T0 - timedelta(days=1), "title": "resume1",
                     "mtime": T0 + timedelta(seconds=30)},
        "r2.jsonl": {"first_ts": T0 - timedelta(days=2), "title": "resume2",
                     "mtime": T0 + timedelta(seconds=30)},  # 同一 mtime
    })
    procs = _procs((100, T0), (200, T0 + timedelta(seconds=10)))
    res = transcripts.resolve_files(procs)
    owners = [v["file"] for v in res.values() if v["file"]]
    assert len(owners) == len(set(owners))  # 排他


def test_round2_picks_latest_mtime_for_same_proc(tmp_path, monkeypatch):
    """MED-2：一个无文件进程面对多个启动后被续写的旧文件，当前文件取 mtime
    最新者，与第一轮"多文件取最新"语义一致。"""
    _setup(tmp_path, monkeypatch, {
        "old1.jsonl": {"first_ts": T0 - timedelta(days=2), "title": "更早续写",
                       "mtime": T0 + timedelta(seconds=20)},
        "old2.jsonl": {"first_ts": T0 - timedelta(days=1), "title": "最新续写",
                       "mtime": T0 + timedelta(seconds=80)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "old2.jsonl"


def test_near_simultaneous_files_not_dominated_by_closer_proc(tmp_path, monkeypatch):
    """现网真实案例（2026-10-16 串台二象）：两个进程间隔 159s 启动，两个会话
    文件首事件只差 4s，且对晚启动进程的 gap 都更小（85/89s vs 244/248s）。
    逐文件 argmin 会让晚启动进程赢走两个文件，早启动会话饿死（列表无记录、
    被误以为是新会话）。最大化被服务进程数 + minimax 必须一对一各回各家。"""
    _setup(tmp_path, monkeypatch, {
        "nihao.jsonl": {"first_ts": T0 + timedelta(seconds=244),
                        "title": "你好", "mtime": T0 + timedelta(minutes=20)},
        "hi.jsonl": {"first_ts": T0 + timedelta(seconds=248),
                     "title": "hi", "mtime": T0 + timedelta(minutes=20)},
    })
    # 3528 在 T0 启动；25936 在 159s 后启动
    procs = _procs((3528, T0), (25936, T0 + timedelta(seconds=159)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 3528)]["file"] == "nihao.jsonl"
    assert res[("ext", 3528)]["title"] == "你好"
    assert res[("ext", 25936)]["file"] == "hi.jsonl"
    assert res[("ext", 25936)]["title"] == "hi"


def test_more_files_than_procs_extra_file_attaches_to_owner(tmp_path, monkeypatch):
    """文件数 > 进程数（一个进程 /clear 开新 session）：1:1 主匹配只给出一个
    文件，另一个按最小 gap 附加给同一属主，当前文件取首事件最新者。"""
    _setup(tmp_path, monkeypatch, {
        "s1.jsonl": {"first_ts": T0 + timedelta(seconds=10),
                     "title": "第一段", "mtime": T0 + timedelta(minutes=9)},
        "s2.jsonl": {"first_ts": T0 + timedelta(seconds=120),
                     "title": "第二段", "mtime": T0 + timedelta(minutes=10)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "s2.jsonl"
    assert res[("ext", 100)]["title"] == "第二段"


def test_round1_owned_old_files_not_stolen_by_round2(tmp_path, monkeypatch):
    """老进程第一轮已认领到的旧 session 文件（/clear 前），不得被第二轮
    分给另一个无文件进程。"""
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0 + timedelta(seconds=30),
                    "title": "新 session", "mtime": T0 + timedelta(minutes=10)},
        "b.jsonl": {"first_ts": T0 + timedelta(seconds=10),
                    "title": "旧 session", "mtime": T0 + timedelta(minutes=9)},
        "r.jsonl": {"first_ts": T0 - timedelta(days=1),
                    "title": "别人 resume 的",
                    "mtime": T0 + timedelta(seconds=70)},
    })
    # 100 在 30s/10s 拥有 a,b；200 是 60s 后启动的无文件进程，r.jsonl 归它
    procs = _procs((100, T0), (200, T0 + timedelta(seconds=60)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "a.jsonl"  # 最新 session
    assert res[("ext", 200)]["file"] == "r.jsonl"
