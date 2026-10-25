# -*- coding: utf-8 -*-
"""ReplayBuffer 偏移数学与等待语义。"""
import threading
import time

from server.replay import ReplayBuffer


def test_append_and_slice():
    b = ReplayBuffer(cap=100)
    b.append("hello ")
    b.append("world")
    assert b.total == 11 and b.oldest == 0
    assert b.slice_from(0) == "hello world"
    assert b.slice_from(6) == "world"
    assert b.slice_from(11) == ""


def test_trim_advances_oldest():
    b = ReplayBuffer(cap=10)
    b.append("0123456789")
    b.append("abcde")  # 超出 5，挤掉 "01234"
    assert b.total == 15 and b.oldest == 5
    assert b.slice_from(5) == "56789abcde"
    assert b.slice_from(4) is None   # 太旧：已被挤掉
    assert b.slice_from(16) is None  # 超前：客户端来自更新的服务端


def test_slice_boundary():
    b = ReplayBuffer(cap=4)
    b.append("abcd")
    b.append("e")  # 挤掉 a
    assert b.oldest == 1
    assert b.slice_from(1) == "bcde"
    assert b.slice_from(5) == ""


def test_wait_from_returns_new_data():
    b = ReplayBuffer()
    b.append("abc")
    r = b.wait_from(0, timeout=0.1)
    assert r == {"chunk": "abc", "total": 3, "gap": False, "eof": False}


def test_wait_from_gap_reports_oldest():
    b = ReplayBuffer(cap=3)
    b.append("abcdef")  # oldest=3
    r = b.wait_from(0, timeout=0.1)
    assert r["gap"] is True and r["oldest"] == 3


def test_wait_from_blocks_until_append():
    b = ReplayBuffer()
    got = {}

    def consumer():
        got.update(b.wait_from(0, timeout=5.0))

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.05)
    b.append("xyz")
    t.join(timeout=2)
    assert got["chunk"] == "xyz" and got["total"] == 3 and not got["gap"]


def test_wait_from_timeout_returns_empty():
    b = ReplayBuffer()
    t0 = time.monotonic()
    r = b.wait_from(0, timeout=0.2)
    assert time.monotonic() - t0 >= 0.15
    assert r["chunk"] == "" and r["gap"] is False and r["eof"] is False


def test_mark_eof_wakes_waiters():
    b = ReplayBuffer()
    got = {}

    def consumer():
        got.update(b.wait_from(0, timeout=5.0))

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.05)
    b.mark_eof()
    t.join(timeout=2)
    assert got["eof"] is True and got["chunk"] == ""


def test_eof_after_data():
    b = ReplayBuffer()
    b.append("tail")
    b.mark_eof()
    r = b.wait_from(0, timeout=0.1)
    assert r["chunk"] == "tail" and r["eof"] is True
