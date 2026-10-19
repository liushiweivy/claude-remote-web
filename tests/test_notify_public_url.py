# -*- coding: utf-8 -*-
"""notify_public_url 日志解析测试。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from notify_public_url import extract_tunnel_url  # noqa: E402

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=TZ)


def _new_tunnel(ts: datetime, url: str, proto: str = "http",
                tunnel: str = "web8765", error: str = "") -> str:
    return (
        f'time="{ts.isoformat()}" level=debug msg=\'Read message '
        f'{{"Type":"NewTunnel","Payload":{{"TunnelName":"{tunnel}",'
        f'"Url":"{url}","Protocol":"{proto}","Error":"{error}"}}}}\''
    )


def test_picks_http_url_of_target_tunnel():
    ts = NOW - timedelta(seconds=10)
    lines = [
        _new_tunnel(ts, "http://aaaa.r39.cpolar.top", tunnel="website"),
        _new_tunnel(ts, "http://bbbb.r39.cpolar.top"),
    ]
    assert extract_tunnel_url(lines, NOW - timedelta(seconds=30)) == \
        "http://bbbb.r39.cpolar.top"


def test_ignores_https_and_error_records():
    ts = NOW - timedelta(seconds=10)
    lines = [
        _new_tunnel(ts, "https://cccc.r39.cpolar.top", proto="https"),
        _new_tunnel(ts, "http://dddd.r39.cpolar.top", error="dial tcp fail"),
    ]
    assert extract_tunnel_url(lines, NOW - timedelta(seconds=30)) is None


def test_ignores_records_before_since_and_returns_newest():
    old = NOW - timedelta(hours=3)
    new = NOW - timedelta(seconds=5)
    lines = [
        _new_tunnel(old, "http://old1.r39.cpolar.top"),
        _new_tunnel(new, "http://new1.r39.cpolar.top"),
        _new_tunnel(new, "http://new2.r39.cpolar.top"),
    ]
    since = NOW - timedelta(seconds=30)
    assert extract_tunnel_url(lines, since) == "http://new2.r39.cpolar.top"


def test_handles_escaped_json_payload():
    # 真实 cpolar 日志：payload 嵌在 msg="..." 里，引号全部转义
    ts = (NOW - timedelta(seconds=10)).isoformat()
    line = (
        f'time="{ts}" level=debug msg="[ctl] Read message '
        '{\\"Type\\":\\"NewTunnel\\",\\"Payload\\":{\\"TunnelName\\":\\"web8765\\",'
        '\\"Url\\":\\"http://esc1.r39.cpolar.top\\",\\"Protocol\\":\\"http\\",'
        '\\"Error\\":\\"\\"}}"'
    )
    assert extract_tunnel_url([line], NOW - timedelta(seconds=30)) == \
        "http://esc1.r39.cpolar.top"


def test_empty_and_garbage_lines():
    assert extract_tunnel_url(["", "garbage", 'time="bad" x'], NOW) is None
