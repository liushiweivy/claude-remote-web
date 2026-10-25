# -*- coding: utf-8 -*-
"""Web Push 完成通知：会话跑完（或等你批准）时推送到手机。

检测：后台线程轮询 ~/.claude/projects/*/*.jsonl 的 mtime，
busy（45s 内有写入）→ idle 跳变时读尾部判定：
- 最后一条 assistant 的 stop_reason 是 end_turn/stop_sequence → 「跑完了」
- 最后是 tool_use（等权限确认）→ 「等你批准」
同文件 180s 内不重复推（借鉴 claudecodeui 的 20s 去重键，放宽到 3 分钟）。

推送：pywebpush + VAPID。密钥对首启生成存 data/vapid.json；
订阅存 data/push_subs.json（endpoint 为主键，404/410 自动清理）。

纯逻辑（tail_status / 状态机 tick）与 IO 分离，可单测。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("crw.push")

IDLE_AFTER_SEC = 45.0   # 多久没写入算"闲下来"
POLL_SEC = 15.0
DEDUP_SEC = 180.0       # 同文件两次推送最小间隔
TAIL_BYTES = 65_536

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VAPID_FILE = DATA_DIR / "vapid.json"
SUBS_FILE = DATA_DIR / "push_subs.json"

PROJECTS_DIR = Path.home() / ".claude" / "projects"


# ---------- VAPID 密钥 ----------

def load_or_create_vapid(path: Path = VAPID_FILE) -> dict:
    if path.is_file():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("private") and d.get("public"):
                return d
        except (ValueError, OSError):
            pass
    from py_vapid import Vapid  # 延迟导入：纯单测不依赖
    v = Vapid()
    v.generate_keys()
    pem = v.private_pem()
    # 公钥给浏览器 applicationServerKey 用：未压缩点 0x04||x||y 的 urlsafe b64（无填充）。
    # 注意 py_vapid 无现成属性（public_key_urlsafe 只存在于部分分支），手动推导。
    n = v.public_key.public_numbers()
    raw = b"\x04" + n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big")
    pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    d = {
        "private": pem.decode().strip() if isinstance(pem, bytes) else pem,
        "public": pub,
    }
    path.write_text(json.dumps(d), encoding="utf-8")
    return d

def vapid_public_key(vapid: dict) -> str:
    pub = vapid.get("public")
    if isinstance(pub, bytes):
        pub = pub.decode()
    return pub


# ---------- 订阅存取 ----------

def load_subs(path: Path = SUBS_FILE) -> list[dict]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (ValueError, OSError):
        return []


def save_sub(sub: dict, path: Path = SUBS_FILE) -> int:
    subs = [s for s in load_subs(path) if s.get("endpoint") != sub.get("endpoint")]
    subs.append(sub)
    path.write_text(json.dumps(subs, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return len(subs)


def remove_sub(endpoint: str, path: Path = SUBS_FILE) -> int:
    subs = [s for s in load_subs(path) if s.get("endpoint") != endpoint]
    path.write_text(json.dumps(subs, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return len(subs)


# ---------- 尾部判定（纯逻辑） ----------

_DONE_REASONS = {"end_turn", "stop_sequence", "max_tokens", "stop"}


def tail_status(tail_text: str) -> tuple[str, str] | None:
    """从 jsonl 尾部文本判定状态。

    返回 ("done", snippet) / ("permission", snippet) / None（信息不足）。
    只认 assistant 消息；最后一条 assistant 是 tool_use 视为等批准。
    """
    last_stop = None
    snippet = ""
    for ln in reversed(tail_text.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue  # 尾部半行（正在写入）跳过
        if d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        last_stop = msg.get("stop_reason")
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = (b.get("text") or "").strip()
                    if t:
                        snippet = t.replace("\n", " ")[:60]
                        break
        break  # 只看最后一条 assistant
    if last_stop is None:
        return None
    if last_stop in _DONE_REASONS:
        return "done", snippet
    if last_stop == "tool_use":
        return "permission", snippet or "有操作待确认"
    return None


def read_tail(path: Path, nbytes: int = TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# ---------- 状态机（纯逻辑） ----------

class CompletionTracker:
    """busy→idle 跳变检测 + 去重。tick() 返回本次应推送的 (key, kind) 列表。"""

    def __init__(self, idle_after: float = IDLE_AFTER_SEC,
                 dedup: float = DEDUP_SEC):
        self.idle_after = idle_after
        self.dedup = dedup
        self._state: dict[str, dict] = {}  # key -> {"busy": bool, "last_push": float}

    def tick(self, items: list[tuple[str, float]], now: float) -> list[str]:
        """items: [(file_key, mtime)]。返回 busy→idle 跳变的 key 列表（已过 dedup）。"""
        fired = []
        for key, mtime in items:
            st = self._state.setdefault(key, {"busy": False, "last_push": 0.0})
            busy = (now - mtime) < self.idle_after
            if st["busy"] and not busy and (now - st["last_push"]) >= self.dedup:
                fired.append(key)
                st["last_push"] = now
            st["busy"] = busy
        return fired


# ---------- 推送发送 ----------

def send_push_all(title: str, body: str, tag: str,
                  vapid: dict | None = None, subs_path: Path = SUBS_FILE) -> int:
    """向全部订阅推送；失效订阅（404/410）自动清理。返回成功数。"""
    subs = load_subs(subs_path)
    if not subs:
        return 0
    vapid = vapid or load_or_create_vapid()
    from pywebpush import WebPushException, webpush  # 延迟导入
    payload = json.dumps({"title": title, "body": body, "tag": tag},
                         ensure_ascii=False)
    ok = 0
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=vapid["private"],
                    vapid_claims={"sub": "mailto:crw@localhost"},
                    timeout=10)
            ok += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                remove_sub(sub.get("endpoint", ""), subs_path)
                log.info("推送订阅失效已清理: %s", (sub.get("endpoint") or "")[:60])
            else:
                log.warning("推送失败(%s): %s", code, e)
        except Exception as e:  # noqa: BLE001 - 单个订阅失败不拖垮整批
            log.warning("推送异常: %s", e)
    return ok


# ---------- 后台巡检 ----------

def _iter_jsonl(projects_dir: Path) -> list[tuple[str, float, Path]]:
    out = []
    try:
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    out.append((f"{proj.name}/{f.name}", f.stat().st_mtime, f))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def watcher_loop(projects_dir: Path = PROJECTS_DIR,
                 subs_path: Path = SUBS_FILE,
                 poll: float = POLL_SEC,
                 tracker: CompletionTracker | None = None,
                 sleep_fn=time.sleep,
                 send_fn=send_push_all,
                 stop: threading.Event | None = None) -> CompletionTracker:
    """常驻巡检：busy→idle 的会话读尾部判定后推送。"""
    tracker = tracker or CompletionTracker()
    stop = stop or threading.Event()
    while not stop.is_set():
        sleep_fn(poll)
        if stop.is_set():
            break
        if not load_subs(subs_path):
            continue  # 没有任何订阅时完全不碰盘
        items = _iter_jsonl(projects_dir)
        now = time.time()
        for key in tracker.tick([(k, m) for k, m, _ in items], now):
            path = next((p for k, _, p in items if k == key), None)
            if path is None:
                continue
            st = tail_status(read_tail(path))
            if not st:
                continue
            kind, snippet = st
            proj = key.split("/", 1)[0]
            if kind == "done":
                title = "✅ 会话跑完了"
            else:
                title = "⏸ 会话等你批准"
            body = f"{proj} · {snippet}" if snippet else proj
            try:
                send_fn(title, body, tag=f"crw-{key}")
            except Exception as e:  # noqa: BLE001 - 推送故障不影响巡检
                log.warning("推送发送失败: %s", e)
    return tracker


def start_watcher() -> threading.Thread:
    t = threading.Thread(target=watcher_loop, daemon=True, name="crw-push")
    t.start()
    return t
