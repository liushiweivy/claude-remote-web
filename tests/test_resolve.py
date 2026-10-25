# -*- coding: utf-8 -*-
"""会话进程 ↔ jsonl 文件全局排他解析的回归测试。

复现的现网 bug：同 cwd 多个 claude 进程时，旧实现让多个会话指向同一个
（最近修改的）jsonl —— 标题重复、transcript 串台、发消息后不知道谁收到。
"""
import json
import os
from datetime import datetime, timezone, timedelta

import pytest

from server import anchors, sessions, transcripts

# 测试基准时间（UTC 存储，与 jsonl timestamp 一致）
T0 = datetime(2026, 10, 16, 0, 34, 28, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_claim_cache():
    """冻结认领缓存按 Anchor 值命中，跨用例相同锚点会串状态，每例清空。"""
    transcripts._clear_claim_cache()
    yield
    transcripts._clear_claim_cache()


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
    transcripts._meta_cache.clear()  # 同名同尺寸假文件跨用例可能命中陈旧头解析


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


# ---------- 注入锚点 phase-0（2026-10-21 二代串台修复） ----------

def _write_events_jsonl(path, events, *, mtime: datetime):
    """造含多条用户事件的 jsonl：events=[(ts, text), ...]，首事件决定 first_ts。"""
    lines = [
        json.dumps({"type": "user", "timestamp": _iso(ts),
                    "message": {"content": text}}, ensure_ascii=False)
        for ts, text in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def _anchor_store(*specs):
    """specs: (key, cwd, text, ts)；测试时间是固定历史时刻，给极长 ttl 防剪枝
    （因此用例不能依赖真实 anchors._now 过期，只可 monkeypatch transcripts._now_utc）。"""
    st = anchors.AnchorStore(default_ttl=10 ** 9)
    for key, cwd, text, ts in specs:
        st.add(key, cwd, text, ts=ts)
    return st


def test_anchor_append_overturns_wrong_guess(tmp_path, monkeypatch):
    """追写锚点翻盘（现网 24580/26340 案）：老进程 4h 后在新文件发言，
    新启动的闲置进程靠第二轮 mtime 偷走文件；锚点必须把文件硬判给注入目标，
    偷文件的进程诚实回到 None。"""
    b_ts = T0 + timedelta(hours=4)
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0 + timedelta(seconds=100), "title": "旧会话",
                    "mtime": T0 + timedelta(minutes=20)},
    })
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(b_ts, "目前效果怎么样")], mtime=b_ts + timedelta(seconds=1))
    procs = _procs((100, T0), (200, T0 + timedelta(hours=4, seconds=-10)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "目前效果怎么样",
         b_ts - timedelta(milliseconds=300)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"
    assert res[("ext", 200)]["file"] is None


def test_anchor_birth_sole_new_file_cjk_doubling(tmp_path, monkeypatch):
    """出生锚点 + 叠字容错（现网 21308/23924 案）：老进程新开会话产生新文件，
    CONIN$ 中文叠字导致文本不一致，但 20s 窗内只有这一个新文件 → 仍归老进程；
    同组的闲置新进程不得偷取。"""
    b_ts = T0 + timedelta(hours=4, minutes=16)
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0 + timedelta(seconds=100), "title": "旧会话",
                    "mtime": T0 + timedelta(minutes=20)},
    })
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(b_ts, "奇奇怪怪我我在在会会话话台台")],
        mtime=b_ts + timedelta(seconds=1))
    procs = _procs((100, T0), (200, T0 + timedelta(hours=4, minutes=10)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "奇怪我在会话台",
         b_ts - timedelta(milliseconds=80)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"
    assert res[("ext", 200)]["file"] is None


def test_anchor_birth_multiple_new_files_require_text(tmp_path, monkeypatch):
    """窗内有两个新文件且文本都对不上（叠字 squeeze 也不匹配）→ 不得瞎认。"""
    b1_ts = T0 + timedelta(hours=4)
    b2_ts = b1_ts + timedelta(seconds=5)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(tmp_path / "C--Users-4v" / "b1.jsonl",
                        [(b1_ts, "完全不同的内容甲")], mtime=b1_ts)
    _write_events_jsonl(tmp_path / "C--Users-4v" / "b2.jsonl",
                        [(b2_ts, "完全不同的内容乙")], mtime=b2_ts)
    procs = _procs((100, T0))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "注入文本XYZ",
         b1_ts - timedelta(milliseconds=100)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] is None


def test_two_anchors_pair_exclusively_by_nearest_event(tmp_path, monkeypatch):
    """同 cwd 两个注入目标、两条几乎同时落盘的用户事件：按事件-锚点时间差
    排他配对，各回各家（广播/连续单发场景）。"""
    t1 = T0 + timedelta(minutes=30)
    t2 = t1 + timedelta(seconds=10)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(tmp_path / "C--Users-4v" / "f1.jsonl",
                        [(t1, "甲会话专属消息")], mtime=t1)
    _write_events_jsonl(tmp_path / "C--Users-4v" / "f2.jsonl",
                        [(t2, "乙会话专属消息")], mtime=t2)
    procs = _procs((100, T0), (200, T0 + timedelta(seconds=5)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "甲会话专属消息",
         t1 - timedelta(milliseconds=100)),
        (("ext", 200), r"C:\Users\4v", "乙会话专属消息",
         t2 - timedelta(milliseconds=100)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "f1.jsonl"
    assert res[("ext", 200)]["file"] == "f2.jsonl"


def test_anchor_does_not_claim_across_cwd(tmp_path, monkeypatch):
    """锚点只在登记的 cwd 内认领文件；别的 cwd 完全不受影响。"""
    for cwd_enc in ("C--Users-4v", "E--1workspace"):
        (tmp_path / cwd_enc).mkdir(exist_ok=True)
    t1 = T0 + timedelta(minutes=5)
    _write_events_jsonl(tmp_path / "E--1workspace" / "f.jsonl",
                        [(t1, "别的项目的消息")], mtime=t1)
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    transcripts._file_cache.clear()
    transcripts._meta_cache.clear()  # 同名同尺寸假文件跨用例可能命中陈旧头解析
    procs = [
        {"key": ("ext", 1), "cwd": r"C:\Users\4v", "started": _iso(T0)},
        {"key": ("ext", 2), "cwd": r"E:\1workspace", "started": _iso(T0)},
    ]
    store = _anchor_store(
        (("ext", 1), r"C:\Users\4v", "别的项目的消息",
         t1 - timedelta(milliseconds=100)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 1)]["file"] is None       # 锚点 cwd 无文件
    assert res[("ext", 2)]["file"] == "f.jsonl"  # 走常规第一轮，未被锚点干预


def test_anchor_claims_file_even_without_anchor_store(tmp_path, monkeypatch):
    """anchors=None（旧调用约定/未回放场景）行为与以前完全一致。"""
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0 + timedelta(seconds=12), "title": "甲",
                    "mtime": T0 + timedelta(minutes=20)},
        "b.jsonl": {"first_ts": T0 + timedelta(seconds=97), "title": "乙",
                    "mtime": T0 + timedelta(minutes=20)},
    })
    procs = _procs((100, T0), (200, T0 + timedelta(seconds=66)))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "a.jsonl"
    assert res[("ext", 200)]["file"] == "b.jsonl"


# ---------- 第二轮 mtime 匹配 30 分钟封顶 ----------

def test_round2_capped_at_lookback_window(tmp_path, monkeypatch):
    """无锚点时，homeless 进程不得靠第二轮无限回溯认领 4h 后才被写的文件。"""
    _setup(tmp_path, monkeypatch, {
        "late.jsonl": {"first_ts": T0 - timedelta(days=1), "title": "迟来的",
                       "mtime": T0 + timedelta(hours=4)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] is None


def test_round2_short_resume_gap_still_claims(tmp_path, monkeypatch):
    """封顶不误伤正常 resume：启动 30s 后续写的旧文件照常匹配。"""
    _setup(tmp_path, monkeypatch, {
        "r.jsonl": {"first_ts": T0 - timedelta(days=1), "title": "resume 的",
                    "mtime": T0 + timedelta(seconds=30)},
    })
    procs = _procs((100, T0))
    res = transcripts.resolve_files(procs)
    assert res[("ext", 100)]["file"] == "r.jsonl"


# ---------- 代码审查回归（2026-10-21 下午，锚点 v2） ----------

def test_replay_anchor_for_dead_pid_does_not_starve_live_proc(tmp_path, monkeypatch):
    """MED-1：回放锚点指向已退出 pid 时，只占池不产结果 —— 它认领的文件
    必须继续留在两轮池里，让同 cwd 存活的 resume 进程正常匹配。"""
    ev_ts = T0 + timedelta(seconds=40)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(tmp_path / "C--Users-4v" / "b.jsonl",
                        [(ev_ts, "回放锚点存活文本XYZ")],
                        mtime=ev_ts + timedelta(seconds=1))
    procs = _procs((200, T0))  # 锚点目标 ("ext", 999) 已死，200 活着
    store = _anchor_store(
        (("ext", 999), r"C:\Users\4v", "回放锚点存活文本XYZ",
         ev_ts - timedelta(milliseconds=300)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 200)]["file"] == "b.jsonl"


def test_birth_claim_requires_text_corroboration(tmp_path, monkeypatch):
    """MED-2：窗内唯一新文件，但首条用户文本与注入文本（含 squeeze）完全
    对不上 → 不得出生认领；文件回到两轮池归真正在本地打字的进程。"""
    b_ts = T0 + timedelta(hours=4, minutes=16)
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": T0 + timedelta(seconds=100), "title": "旧会话",
                    "mtime": T0 + timedelta(minutes=20)},
    })
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(b_ts, "本地手打的第一条完全不同内容")],
        mtime=b_ts + timedelta(seconds=1))
    procs = _procs((100, T0), (200, T0 + timedelta(hours=4, minutes=10)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "注入串台测试XYZ",
         b_ts - timedelta(milliseconds=80)))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "a.jsonl"  # 锚点未碰 b，旧会话仍归 100
    assert res[("ext", 200)]["file"] == "b.jsonl"  # 本地新文件回到真主人


def _patch_clock(monkeypatch, now):
    monkeypatch.setattr(transcripts, "_now_utc", lambda: now)


def test_closed_window_anchor_freezes_claim_without_rescan(tmp_path, monkeypatch):
    """MED-3：匹配窗关闭后锚点决定冻结（含正/负结果），后续 resolve 不再
    整扫任何文件；冻结的认领照样把文件硬判给注入目标。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": now0 - timedelta(hours=4), "title": "旧会话",
                    "mtime": now0 - timedelta(hours=3, minutes=40)},
    })
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0, "冻结锚点测试消息ABC")], mtime=now0 + timedelta(seconds=1))
    procs = _procs((100, now0 - timedelta(hours=4)),
                   (200, now0 - timedelta(seconds=30)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "冻结锚点测试消息ABC",
         now0 - timedelta(milliseconds=300)))

    scans: list[str] = []
    real_read = transcripts.read_user_events

    def counting_read(cwd, file):
        scans.append(file)
        return real_read(cwd, file)

    monkeypatch.setattr(transcripts, "read_user_events", counting_read)

    # t1：窗还开着（注入后 1s），事件已落盘 → 文本边认领，整扫发生
    _patch_clock(monkeypatch, now0 + timedelta(seconds=1))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"
    assert res[("ext", 200)]["file"] is None
    assert scans == ["b.jsonl"]

    # 文件继续被写（活跃会话），t2 窗刚关闭 → 冻结正认领
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0, "冻结锚点测试消息ABC"),
         (now0 + timedelta(seconds=30), "后续对话另一条内容")],
        mtime=now0 + timedelta(seconds=30))
    scans.clear()
    _patch_clock(monkeypatch, now0 + timedelta(seconds=25))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"
    assert res[("ext", 200)]["file"] is None

    # t3：此后每个轮询都直接吃缓存，零整扫 —— 15min TTL 内不再有热路径成本
    scans.clear()
    _patch_clock(monkeypatch, now0 + timedelta(seconds=60))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"
    assert res[("ext", 200)]["file"] is None
    assert scans == []


def test_frozen_negative_anchor_not_rescanned(tmp_path, monkeypatch):
    """MED-3：窗关闭时没有任何文本对上的锚点冻结为"无认领"（负缓存），
    后续轮询零整扫，也不会随文件续写突然乱认。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0, "完全不相干的本地对话内容")],
        mtime=now0 + timedelta(seconds=1))
    procs = _procs((100, now0 - timedelta(hours=4)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "另一条注入文本XYZ",
         now0 - timedelta(milliseconds=300)))
    scans: list[str] = []
    real_read = transcripts.read_user_events
    monkeypatch.setattr(
        transcripts, "read_user_events",
        lambda cwd, file: (scans.append(file), real_read(cwd, file))[1])

    _patch_clock(monkeypatch, now0 + timedelta(seconds=25))  # 已关窗
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] is None
    assert scans == ["b.jsonl"]

    scans.clear()
    _patch_clock(monkeypatch, now0 + timedelta(seconds=60))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] is None
    assert scans == []


def test_frozen_claim_released_when_owner_pid_exits(tmp_path, monkeypatch):
    """冻结认领只对仍存活的属主有效：属主进程退出后，文件回到池里，
    交给同 cwd 下真正还活着的进程（锚点按组内 live key 过滤）。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0, "属主退出测试消息ABC")], mtime=now0 + timedelta(seconds=1))
    both = _procs((100, now0 - timedelta(hours=4)),
                  (200, now0 - timedelta(seconds=30)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "属主退出测试消息ABC",
         now0 - timedelta(milliseconds=300)))

    _patch_clock(monkeypatch, now0 + timedelta(seconds=25))
    res = transcripts.resolve_files(both, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"  # 已冻结

    alive = _procs((200, now0 - timedelta(seconds=30)))  # 100 已退出
    res = transcripts.resolve_files(alive, anchors=store)
    assert res[("ext", 200)]["file"] == "b.jsonl"


def test_frozen_claim_skipped_when_file_gone(tmp_path, monkeypatch):
    """冻结认领指向的文件已被删除（会话清理）→ 跳过，不得产生幽灵归属，
    其余文件照常走两轮匹配。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    proj = tmp_path / "C--Users-4v"
    _setup(tmp_path, monkeypatch, {
        "a.jsonl": {"first_ts": now0 - timedelta(seconds=90), "title": "甲",
                    "mtime": now0 - timedelta(seconds=80)},
    })
    _write_events_jsonl(proj / "b.jsonl",
                        [(now0, "文件即将消失测试消息")],
                        mtime=now0 + timedelta(seconds=1))
    procs = _procs((100, now0 - timedelta(seconds=100)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "文件即将消失测试消息",
         now0 - timedelta(milliseconds=300)))

    _patch_clock(monkeypatch, now0 + timedelta(seconds=25))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"

    (proj / "b.jsonl").unlink()
    transcripts._file_cache.clear()
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "a.jsonl"


# ---------- 代码审查回归（2026-10-21 晚，冻结缓存边界） ----------

def test_negative_freeze_waits_grace_for_late_flush(tmp_path, monkeypatch):
    """负冻结宽限：事件时间戳落在窗内（ts+19.9s）但落盘晚于首个关窗
    轮询（ts+21s）时，None 不得立即冻结 —— 宽限内事件补扫到必须能认。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0 + timedelta(seconds=1), "无关本地对话内容")],
        mtime=now0 + timedelta(seconds=1))
    procs = _procs((100, now0 - timedelta(hours=4)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "宽限补扫测试消息ABC", now0))
    anchor = store.recent(r"C:\Users\4v")[0]

    # t1 = 注入后 21s：窗刚关 1s，尚无匹配事件 → 不认，但负结果也不冻结
    _patch_clock(monkeypatch, now0 + timedelta(seconds=21))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] is None
    assert anchor not in transcripts._claim_cache

    # 事件（时间戳 ts+19.9s，窗内）此刻才刷盘
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0 + timedelta(seconds=1), "无关本地对话内容"),
         (now0 + timedelta(seconds=19, milliseconds=900), "宽限补扫测试消息ABC")],
        mtime=now0 + timedelta(seconds=22))

    # t2 = 22s（仍在 +5s 宽限内）→ 补扫到窗内事件，正认领并冻结
    _patch_clock(monkeypatch, now0 + timedelta(seconds=22))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] == "b.jsonl"
    assert transcripts._claim_cache.get(anchor) == "b.jsonl"


def test_negative_freezes_after_grace_and_holds(tmp_path, monkeypatch):
    """宽限（+5s）过后仍无匹配事件才冻结 None；冻结后零整扫且不乱认。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    _setup(tmp_path, monkeypatch, {})
    _write_events_jsonl(
        tmp_path / "C--Users-4v" / "b.jsonl",
        [(now0 + timedelta(seconds=1), "无关本地对话内容")],
        mtime=now0 + timedelta(seconds=1))
    procs = _procs((100, now0 - timedelta(hours=4)))
    store = _anchor_store(
        (("ext", 100), r"C:\Users\4v", "宽限后冻结测试消息ABC",
         now0 - timedelta(milliseconds=200)))
    anchor = store.recent(r"C:\Users\4v")[0]

    scans: list[str] = []
    real_read = transcripts.read_user_events
    monkeypatch.setattr(
        transcripts, "read_user_events",
        lambda cwd, file: (scans.append(file), real_read(cwd, file))[1])

    _patch_clock(monkeypatch, now0 + timedelta(seconds=26))  # 已过 25s 宽限
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] is None
    assert transcripts._claim_cache.get(anchor) is None

    scans.clear()
    _patch_clock(monkeypatch, now0 + timedelta(seconds=60))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 100)]["file"] is None
    assert scans == []  # 负冻结生效，零整扫


def test_commit_frozen_sticky_positive_never_downgraded():
    """并发：迟到的陈旧负扫描（None）不得覆盖先到的正认领；None→正允许升级。"""
    now = datetime.now(timezone.utc)
    anchor = anchors.Anchor(
        key=("ext", 1), cwd="", ts=now, text="x",
        expires_at=now + timedelta(hours=1))
    transcripts._commit_frozen({anchor: "b.jsonl"}, now)
    assert transcripts._claim_cache[anchor] == "b.jsonl"
    transcripts._commit_frozen({anchor: None}, now + timedelta(seconds=1))
    assert transcripts._claim_cache[anchor] == "b.jsonl"  # 正认领不被降级

    other = anchors.Anchor(
        key=("ext", 2), cwd="", ts=now, text="y",
        expires_at=now + timedelta(hours=1))
    transcripts._commit_frozen({other: None}, now)
    transcripts._commit_frozen({other: "c.jsonl"}, now + timedelta(seconds=1))
    assert transcripts._claim_cache[other] == "c.jsonl"  # 负→正允许升级


def test_anchor_resolves_session_with_unknown_start_time(tmp_path, monkeypatch):
    """started=None（PEB 取不到启动时间）的进程，其锚点仍须能硬认领文件：
    锚点自带属主 key，不依赖时间轮。"""
    now0 = datetime.now(timezone.utc).replace(microsecond=0)
    proj = tmp_path / "C--Users-4v"
    proj.mkdir()
    _write_events_jsonl(proj / "b.jsonl",
                        [(now0 + timedelta(milliseconds=300), "无启动时间锚点测试ABC")],
                        mtime=now0 + timedelta(seconds=1))
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    transcripts._file_cache.clear()
    transcripts._meta_cache.clear()
    procs = [{"key": ("ext", 77), "cwd": r"C:\Users\4v", "started": None}]
    store = _anchor_store(
        (("ext", 77), r"C:\Users\4v", "无启动时间锚点测试ABC", now0))
    _patch_clock(monkeypatch, now0 + timedelta(seconds=1))
    res = transcripts.resolve_files(procs, anchors=store)
    assert res[("ext", 77)]["file"] == "b.jsonl"
