# -*- coding: utf-8 -*-
"""代理本机 quota-monitor（127.0.0.1:8771）的套餐额度快照与 CC KEY 操作。

会话台可能经 cpolar 公网访问，远端浏览器够不到 quota-monitor 的回环地址，
由这里在服务端取数；各 /api 复用会话台 token 鉴权，给无鉴权的额度服务
挡一道。目标地址只来自环境变量（非用户输入），没有 SSRF 面。

上游本身有 540s 缓存，这里不再缓存；urllib 是阻塞调用，app.py 用同步 def
挂路由（FastAPI 自动放线程池），不卡事件循环。

上游形态（2026-10-17 起为双账号版）：
  新：{"accounts": [{account_role, items, ok, stale, error, ...}], "cc_switch": {...}}
  旧：{"items": [...], "ok", "stale", "error"}
parse_snapshot 两种都收，统一规整出顶层 items（当前 cc key 对应账号的套餐，
徽标直接吃它），accounts / cc_switch 原样透传给面板。
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

QUOTA_URL = os.environ.get("CRW_QUOTA_URL", "http://127.0.0.1:8771/api/quota")
TIMEOUT_S = 4.0


def _upstream_base() -> str:
    """从 QUOTA_URL 推额度服务根地址（动作接口与 /api/quota 同源）。

    只保留 scheme+netloc：CRW_QUOTA_URL 若带路径前缀（反代子路径场景），
    动作接口会拼错地址——当前仅支持 quota-monitor 裸根路径部署。"""
    parts = urlsplit(QUOTA_URL)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _with_force(url: str, force: bool) -> str:
    """force=True 时追加 force=1，让 quota-monitor 绕过其 540s 缓存强取。"""
    if not force:
        return url
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query else "") + "force=1"
    return urlunsplit(parts._replace(query=query))


def _failed(error: str) -> dict:
    return {"items": [], "accounts": [], "cc_switch": None,
            "ok": False, "stale": False, "error": error}


def _pick_display_account(accounts: list[dict], cc_switch: dict | None) -> dict:
    """徽标展示账号 = 当前 cc key 对应的账号（切到备 key 就该看备账号用量）。

    mode 非 builtin（自定义 key）时不知道对应谁，回退主账号。
    """
    mode = (cc_switch or {}).get("mode")
    if mode in ("primary", "backup"):
        for acc in accounts:
            if acc.get("account_role") == mode:
                return acc
    for acc in accounts:
        if acc.get("account_role") == "primary":
            return acc
    return accounts[0]


def parse_snapshot(raw: str | bytes) -> dict:
    """校验上游 JSON 形态并规整；畸形一律降级为 ok=False，绝不向调用方抛异常。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return _failed("额度服务返回内容无法解析")
    if not isinstance(data, dict):
        return _failed("额度服务返回格式异常")

    accounts = data.get("accounts")
    if accounts is not None:
        # 新双账号形态：accounts 必须是非空数组，元素带 items
        if (not isinstance(accounts, list) or not accounts
                or not all(isinstance(a, dict) and isinstance(a.get("items"), list)
                           for a in accounts)):
            return _failed("额度服务返回格式异常")
        cc_switch = data.get("cc_switch")
        display = _pick_display_account(accounts, cc_switch)
        return {
            "items": display["items"],
            "display_role": display.get("account_role", "primary"),
            "accounts": accounts,
            "cc_switch": cc_switch if isinstance(cc_switch, dict) else None,
            "ok": bool(display.get("ok", True)),
            "stale": bool(display.get("stale", False)),
            "error": str(display.get("error") or ""),
        }

    # 旧单账号形态（兼容 quota-monitor 未升级的场景）
    if not isinstance(data.get("items"), list):
        return _failed("额度服务返回格式异常")
    data.setdefault("ok", True)
    data.setdefault("stale", False)
    data.setdefault("error", "")
    data.setdefault("accounts", [])
    data.setdefault("cc_switch", None)
    data.setdefault("display_role", "primary")
    return data


def _open(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def fetch_snapshot(url: str = QUOTA_URL, timeout: float = TIMEOUT_S,
                   force: bool = False) -> dict:
    """取一次额度快照；连接失败/超时/HTTP 错误/坏 JSON 均返回 ok=False。

    force=True 透传 force=1 给上游，绕过其 540s 缓存立即取新（手动刷新按钮）。
    """
    try:
        raw = _open(_with_force(url, force), timeout)
    except urllib.error.HTTPError as e:
        return _failed(f"额度服务返回错误（HTTP {e.code}）")
    except (urllib.error.URLError, OSError):
        # URLError 是 OSError 子类（TimeoutError 3.10+ 也是）；未启动/拒连/超时落这里
        return _failed("额度服务不可用（未启动或响应超时）")
    except http.client.HTTPException:
        # IncompleteRead 等读 body 阶段的协议错误不是 OSError，urlopen 不包装
        return _failed("额度服务响应中断")
    return parse_snapshot(raw)


def _error_detail(body: bytes, fallback: str) -> str:
    """从上游错误响应体里挖 detail 文案（FastAPI 的 400 都是 {"detail": ...}）。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fallback
    if isinstance(data, dict) and isinstance(data.get("detail"), str):
        return data["detail"]
    return fallback


def proxy_action(path: str, method: str = "GET", body: dict | None = None,
                 timeout: float = TIMEOUT_S) -> tuple[int, dict]:
    """转发 CC KEY / 隧道等动作到 quota-monitor，返回 (HTTP 状态码, JSON dict)。

    上游 4xx 连同 detail 原文透传（如「内置 key 不可删除」）；上游不可达
    一律 502。path 只能是本文件写死的常量，不接用户输入。
    """
    url = _upstream_base() + path
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = _error_detail(e.read(), f"额度服务返回错误（HTTP {e.code}）")
        return e.code if 400 <= e.code < 500 else 502, {"detail": detail}
    except (urllib.error.URLError, OSError):
        return 502, {"detail": "额度服务不可用（未启动或响应超时）"}
    except http.client.HTTPException:
        return 502, {"detail": "额度服务响应中断"}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 502, {"detail": "额度服务返回内容无法解析"}
    if not isinstance(data, dict):
        return 502, {"detail": "额度服务返回格式异常"}
    return 200, data
