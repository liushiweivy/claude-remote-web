# -*- coding: utf-8 -*-
"""quota-monitor 代理：JSON 校验 / 故障降级 / API 鉴权。"""
import json

import pytest
from fastapi.testclient import TestClient

from server import quota
from server.app import TOKEN, app

client = TestClient(app)

SAMPLE = {
    "items": [{
        "product": "coding-plan", "display_name": "Coding Plan",
        "subscribed": True, "error": None,
        "periods": [
            {"label": "session", "used": None, "total": None, "percent": 2.19,
             "reset_at": "2026-10-16T18:45:57+08:00"},
            {"label": "weekly", "used": None, "total": None, "percent": 66.17,
             "reset_at": "2026-10-19T00:00:00+08:00"},
        ],
    }],
    "viewer_name": "tester", "fetched_at": 1.0,
    "ok": True, "stale": False, "error": "",
}


# ---------- parse_snapshot ----------

def test_parse_snapshot_passthrough():
    d = quota.parse_snapshot(json.dumps(SAMPLE))
    assert d["ok"] is True
    assert d["items"][0]["periods"][0]["percent"] == 2.19


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"ok": True}),            # 缺 items
    json.dumps({"items": "x"}),          # items 不是数组
])
def test_parse_snapshot_bad_shape(raw):
    d = quota.parse_snapshot(raw)
    assert d["ok"] is False and d["items"] == [] and d["error"]


# ---------- fetch_snapshot ----------

def test_fetch_snapshot_success(monkeypatch):
    monkeypatch.setattr(
        quota, "_open", lambda url, timeout: json.dumps(SAMPLE).encode("utf-8"))
    d = quota.fetch_snapshot()
    assert d["ok"] is True and d["items"][0]["product"] == "coding-plan"


def test_fetch_snapshot_unreachable(monkeypatch):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(quota, "_open", boom)
    d = quota.fetch_snapshot()
    assert d["ok"] is False and d["items"] == [] and d["error"]


def test_fetch_snapshot_bad_body(monkeypatch):
    monkeypatch.setattr(quota, "_open", lambda url, timeout: b"oops")
    d = quota.fetch_snapshot()
    assert d["ok"] is False and d["error"]


def test_fetch_snapshot_default_no_force(monkeypatch):
    seen = {}

    def fake_open(url, timeout):
        seen["url"] = url
        return json.dumps(SAMPLE).encode("utf-8")

    monkeypatch.setattr(quota, "_open", fake_open)
    quota.fetch_snapshot("http://127.0.0.1:8771/api/quota")
    assert "force=" not in seen["url"]


def test_fetch_snapshot_force_appends_query(monkeypatch):
    seen = {}

    def fake_open(url, timeout):
        seen["url"] = url
        return json.dumps(SAMPLE).encode("utf-8")

    monkeypatch.setattr(quota, "_open", fake_open)
    quota.fetch_snapshot("http://127.0.0.1:8771/api/quota", force=True)
    assert seen["url"] == "http://127.0.0.1:8771/api/quota?force=1"


def test_fetch_snapshot_force_keeps_existing_query(monkeypatch):
    seen = {}

    def fake_open(url, timeout):
        seen["url"] = url
        return json.dumps(SAMPLE).encode("utf-8")

    monkeypatch.setattr(quota, "_open", fake_open)
    quota.fetch_snapshot("http://127.0.0.1:8771/api/quota?x=1", force=True)
    assert seen["url"] == "http://127.0.0.1:8771/api/quota?x=1&force=1"


def test_fetch_snapshot_http_error(monkeypatch):
    # 上游返回 5xx：HTTPError 必须降级为 ok=false 并带状态码，不让 500 穿透
    import urllib.error

    def boom(url, timeout):
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)

    monkeypatch.setattr(quota, "_open", boom)
    d = quota.fetch_snapshot()
    assert d["ok"] is False and d["items"] == [] and "HTTP 503" in d["error"]


def test_fetch_snapshot_incomplete_read(monkeypatch):
    # 上游响应头后中途断连：IncompleteRead 是 http.client.HTTPException 而非
    # OSError，必须也被降级，不能让 500 穿透
    import http.client

    def boom(url, timeout):
        raise http.client.IncompleteRead(b'{"items"')

    monkeypatch.setattr(quota, "_open", boom)
    d = quota.fetch_snapshot()
    assert d["ok"] is False and d["items"] == [] and "中断" in d["error"]


# ---------- /api/quota ----------

def test_quota_endpoint_requires_token():
    assert client.get("/api/quota").status_code == 401
    assert client.get("/api/quota", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_quota_endpoint_proxies(monkeypatch):
    monkeypatch.setattr(quota, "fetch_snapshot", lambda force=False: SAMPLE)
    r = client.get(f"/api/quota?token={TOKEN}")
    assert r.status_code == 200
    assert r.json()["items"][0]["periods"][0]["percent"] == 2.19


def test_quota_endpoint_passes_force(monkeypatch):
    calls = []

    def fake_fetch(force=False):
        calls.append(force)
        return SAMPLE

    monkeypatch.setattr(quota, "fetch_snapshot", fake_fetch)
    r = client.get(f"/api/quota?token={TOKEN}&force=1")
    assert r.status_code == 200
    assert calls == [True]


def test_quota_endpoint_degrades(monkeypatch):
    monkeypatch.setattr(
        quota, "fetch_snapshot",
        lambda force=False: {"items": [], "ok": False, "stale": False, "error": "额度服务不可用"})
    r = client.get(f"/api/quota?token={TOKEN}")
    # 上游挂了也返回 200 + ok=false，由徽标渲染降级态（与 quota-monitor 约定一致）
    assert r.status_code == 200 and r.json()["ok"] is False


# ---------- 双账号新形态（quota-monitor 2026-10-17 起） ----------

def _acc(role, weekly_pct, ok=True, stale=False, error=""):
    return {
        "items": [{
            "product": "coding-plan", "display_name": "Coding Plan",
            "subscribed": True, "error": None,
            "periods": [
                {"label": "session", "used": None, "total": None,
                 "percent": 1.0, "reset_at": None},
                {"label": "weekly", "used": None, "total": None,
                 "percent": weekly_pct, "reset_at": None},
            ],
        }],
        "viewer_name": "tester-" + role, "ok": ok, "stale": stale,
        "error": error, "account_id": "1" if role == "primary" else "2",
        "account_role": role,
    }


CC_SWITCH = {
    "enabled": True, "auto_enabled": False, "mode": "primary",
    "percent": 0.0, "threshold": 90.0, "switched_at": None, "error": None,
    "keys": [
        {"id": "primary", "label": "主 key", "builtin": True,
         "current": True, "token_tail": "…03d41"},
        {"id": "backup", "label": "备 key", "builtin": True,
         "current": False, "token_tail": "…f844e"},
    ],
}


def dual_sample(mode="primary"):
    cc = dict(CC_SWITCH, mode=mode)
    cc["keys"] = [dict(k, current=(k["id"] == mode)) for k in CC_SWITCH["keys"]]
    return {"accounts": [_acc("primary", 66.0), _acc("backup", 21.0)],
            "cc_switch": cc}


def test_parse_dual_shape_picks_primary_by_default():
    d = quota.parse_snapshot(json.dumps(dual_sample()))
    assert d["ok"] is True
    assert d["display_role"] == "primary"
    assert d["items"][0]["periods"][1]["percent"] == 66.0
    assert len(d["accounts"]) == 2
    assert d["cc_switch"]["mode"] == "primary"


def test_parse_dual_shape_follows_current_key():
    # 当前 key 切到备：徽标该看备账号用量，而不是仍盯主账号
    d = quota.parse_snapshot(json.dumps(dual_sample(mode="backup")))
    assert d["display_role"] == "backup"
    assert d["items"][0]["periods"][1]["percent"] == 21.0


def test_parse_dual_shape_custom_key_falls_back_primary():
    # 自定义 key 不知道对应哪个账号，回退主账号展示
    d = quota.parse_snapshot(json.dumps(dual_sample(mode="abcd1234")))
    assert d["display_role"] == "primary"


def test_parse_dual_shape_degraded_account():
    # 展示账号自己取数失败：透传它的 ok/stale/error，不伪装成功
    bad = dual_sample()
    bad["accounts"][0] = _acc("primary", 0.0, ok=False, stale=True, error="arkcli 超时")
    d = quota.parse_snapshot(json.dumps(bad))
    assert d["ok"] is False and d["stale"] is True and d["error"] == "arkcli 超时"


@pytest.mark.parametrize("raw", [
    json.dumps({"accounts": []}),                 # 空账号数组
    json.dumps({"accounts": "x"}),                # 不是数组
    json.dumps({"accounts": [{"ok": True}]}),     # 账号缺 items
    json.dumps([1, 2]),                           # 顶层不是 dict
])
def test_parse_dual_shape_bad(raw):
    d = quota.parse_snapshot(raw)
    assert d["ok"] is False and d["items"] == [] and d["error"]


# ---------- proxy_action ----------

def _fake_response(payload, code=200):
    import io

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    r = Resp(json.dumps(payload).encode("utf-8"))
    r.code = code
    return r


def test_proxy_action_success(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        return _fake_response({"enabled": True, "auto_enabled": True})

    monkeypatch.setattr(quota.urllib.request, "urlopen", fake_urlopen)
    status, data = quota.proxy_action("/api/cc-switch/auto", "POST", {"enabled": True})
    assert status == 200 and data["auto_enabled"] is True
    assert seen["url"] == "http://127.0.0.1:8771/api/cc-switch/auto"
    assert seen["method"] == "POST"
    assert json.loads(seen["body"].decode()) == {"enabled": True}


def test_proxy_action_upstream_400_passthrough(monkeypatch):
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {},
            _fake_response({"detail": "内置 key 不可删除"}))

    monkeypatch.setattr(quota.urllib.request, "urlopen", boom)
    status, data = quota.proxy_action("/api/cc-keys/primary", "DELETE")
    # 上游 4xx 原文透传（前端 toast 要能看到具体原因），不吞成 502
    assert status == 400 and data["detail"] == "内置 key 不可删除"


def test_proxy_action_unreachable(monkeypatch):
    def boom(req, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(quota.urllib.request, "urlopen", boom)
    status, data = quota.proxy_action("/api/tunnels")
    assert status == 502 and "不可用" in data["detail"]


def test_proxy_action_bad_json(monkeypatch):
    monkeypatch.setattr(quota.urllib.request, "urlopen",
                        lambda req, timeout: _fake_response({}).__class__(b"oops"))
    status, data = quota.proxy_action("/api/tunnels")
    assert status == 502 and "无法解析" in data["detail"]


# ---------- CC KEY / 隧道端点（鉴权 + 参数校验） ----------

def test_cc_endpoints_require_token():
    assert client.post("/api/cc-switch/auto", json={"enabled": True}).status_code == 401
    assert client.post("/api/cc-switch/mode", json={"key_id": "backup"}).status_code == 401
    assert client.post("/api/cc-keys", json={"label": "x", "token": "ark-y"}).status_code == 401
    assert client.delete("/api/cc-keys/abcd1234").status_code == 401
    assert client.get("/api/tunnels").status_code == 401


def test_cc_switch_auto_passes_through(monkeypatch):
    calls = []

    def fake_proxy(path, method="GET", body=None):
        calls.append((path, method, body))
        return 200, {"enabled": True, "auto_enabled": True, "keys": []}

    monkeypatch.setattr(quota, "proxy_action", fake_proxy)
    r = client.post("/api/cc-switch/auto", json={"enabled": True},
                    headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200 and r.json()["auto_enabled"] is True
    assert calls == [("/api/cc-switch/auto", "POST", {"enabled": True})]


def test_cc_switch_mode_validates_key_id(monkeypatch):
    monkeypatch.setattr(quota, "proxy_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应到上游")))
    r = client.post("/api/cc-switch/mode", json={"key_id": "../etc"},
                    headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 400


def test_cc_switch_mode_upstream_error_passthrough(monkeypatch):
    monkeypatch.setattr(quota, "proxy_action",
                        lambda *a, **k: (400, {"detail": "未知 key：xyz"}))
    r = client.post("/api/cc-switch/mode", json={"key_id": "xyz"},
                    headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 400 and r.json()["detail"] == "未知 key：xyz"


def test_add_cc_key_validates_body(monkeypatch):
    monkeypatch.setattr(quota, "proxy_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应到上游")))
    for body in ({"label": "  ", "token": "ark-x"},
                 {"label": "x", "token": ""},
                 {"label": "x" * 41, "token": "ark-x"},
                 {"label": "x", "token": "ark-" + "y" * 200}):
        r = client.post("/api/cc-keys", json=body, headers={"X-Auth-Token": TOKEN})
        assert r.status_code == 400, body


def test_delete_cc_key_validates_id():
    # 含正则外字符的 id 到不了上游（%2F 之类连路由都匹配不上，是 404 也安全）
    r = client.delete("/api/cc-keys/a!b", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 400


def test_tunnels_endpoint_success(monkeypatch):
    monkeypatch.setattr(quota, "proxy_action",
                        lambda *a, **k: (200, {"tunnels": [
                            {"name": "web8765", "label": "Claude 会话台",
                             "url": "https://x.cpolar.top/?token=1234",
                             "is_http": True, "note": ""}]}))
    r = client.get("/api/tunnels", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["tunnels"][0]["name"] == "web8765"


def test_tunnels_endpoint_degrades(monkeypatch):
    monkeypatch.setattr(quota, "proxy_action",
                        lambda *a, **k: (502, {"detail": "额度服务不可用（未启动或响应超时）"}))
    r = client.get("/api/tunnels", headers={"X-Auth-Token": TOKEN})
    # 与 /api/quota 同约定：上游挂了也 200 + ok=false，面板渲染降级态
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["tunnels"] == []
