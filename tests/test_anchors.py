# -*- coding: utf-8 -*-
"""注入锚点内存表：TTL/容量/文本匹配（含 CONIN$ 中文叠字容错）/ cwd 隔离。"""
from datetime import datetime, timedelta, timezone

from server import anchors

T0 = datetime(2026, 10, 21, 4, 41, 0, tzinfo=timezone.utc)
CWD = r"C:\Users\4v"


def _store(default_ttl=3600.0):
    return anchors.AnchorStore(default_ttl=default_ttl)


def test_add_and_recent_cwd_scoped():
    st = _store()
    st.add(("ext", 1), CWD, "你好", ts=T0)
    st.add(("ext", 2), r"E:\other", "别处", ts=T0)
    recent = st.recent(CWD, now=T0 + timedelta(seconds=1))
    assert len(recent) == 1
    assert recent[0].key == ("ext", 1) and recent[0].text == "你好"
    assert st.recent(r"E:\other", now=T0)[0].key == ("ext", 2)


def test_expired_anchors_pruned():
    st = _store(default_ttl=900.0)
    st.add(("ext", 1), CWD, "老消息", ts=T0)
    assert st.recent(CWD, now=T0 + timedelta(seconds=899))
    assert st.recent(CWD, now=T0 + timedelta(seconds=901)) == []


def test_cap_evicts_oldest():
    st = _store()
    for i in range(anchors.MAX_ANCHORS + 10):
        st.add(("ext", i), CWD, f"消息{i}", ts=T0 + timedelta(seconds=i))
    recent = st.recent(CWD, now=T0 + timedelta(days=1))
    assert len(recent) <= anchors.MAX_ANCHORS
    # 最老的 10 条被挤出
    assert all(a.key != ("ext", 0) for a in recent)


def test_replay_entries_keep_row_ttl():
    """冷启动回放的锚点按行内时间 + 回放窗口过期，不按启动时刻重置。"""
    st = anchors.AnchorStore(default_ttl=900.0)
    row_ts = T0 - timedelta(hours=1, seconds=5)
    st.add(("ext", 1), CWD, "两小时内的注入", ts=row_ts,
           ttl=anchors.REPLAY_WINDOW_SEC)
    # 行龄 1h：仍在 2h 回放窗口内，可见
    assert st.recent(CWD, now=T0)
    # 超过注入后 2h：即使 default_ttl 很长也必须过期
    assert st.recent(CWD, now=T0 + timedelta(hours=1, seconds=10)) == []


def test_clear():
    st = _store()
    st.add(("ext", 1), CWD, "x", ts=T0)
    st.clear()
    assert st.recent(CWD, now=T0) == []


# ---------- 文本匹配 ----------

def test_text_matches_prefix_and_whitespace():
    assert anchors.text_matches("继续干活", "继续干活，看看效果")
    assert anchors.text_matches("继续 干活\n", "继续 干活，看看效果")  # 归一化空白
    assert anchors.text_matches("a" * 40, "a" * 40 + "后缀")
    assert not anchors.text_matches("继续干活", "完全不同的内容")
    assert not anchors.text_matches("", "内容")
    assert not anchors.text_matches("内容", "")


def test_text_matches_cjk_doubling_fallback():
    """CONIN$ 注入中文在 Claude TUI 可能逐字叠写（libuv 双消费），
    出生锚点的唯一新文件豁免之外，追写锚点也要能兜住叠字文本。"""
    assert anchors.text_matches("继续干活", "继继续续干干活活")
    assert anchors.text_matches("汇报进度", "汇汇报报进进度度啦")  # 前缀比对
    assert not anchors.text_matches("继续干活", "完全不同的句子啊")


def test_clean_pty_payload():
    assert anchors.clean_payload("\x1b[200~第一行\n第二行\x1b[201~\r") == "第一行\n第二行"
    assert anchors.clean_payload("hello\r") == "hello"
