# -*- coding: utf-8 -*-
"""自察看门狗：连续自探失败自杀、成功清零连败（2026-10-18 自救需求）。

进程级卡死外部救不了，只能换进程：watchdog 自杀后由自启 bat 重试循环拉起。
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from server.app import self_health_ok, watchdog_loop


class _Bail(Exception):
    pass


def _run(probes, max_failures=3):
    """按 probes 剧本跑 watchdog_loop，返回 exit_fn 收到的退出码列表。"""
    exits = []
    script = iter(probes)

    def exit_fn(code):
        exits.append(code)
        raise _Bail

    try:
        watchdog_loop(8765, 0, max_failures, exit_fn,
                      sleep_fn=lambda s: None,
                      probe_fn=lambda: next(script))
    except (_Bail, StopIteration):
        pass
    return exits


def test_suicide_after_consecutive_failures():
    assert _run([False, False, False]) == [1]


def test_success_resets_failure_streak():
    # 连败 2 → 成功清零 → 再连败 2：永不达 3 连败，不自杀
    probes = [False, False, True] * 3
    assert _run(probes) == []


def test_healthy_never_exits():
    assert _run([True] * 10) == []


def test_custom_max_failures():
    assert _run([False], max_failures=1) == [1]
    assert _run([True, False], max_failures=2) == []


def test_self_health_ok_against_real_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert self_health_ok(srv.server_port)
    finally:
        srv.shutdown()
        srv.server_close()
    assert not self_health_ok(srv.server_port, timeout=0.5)
