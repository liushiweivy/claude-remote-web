# -*- coding: utf-8 -*-
"""Claude 会话台：查看本机全部 claude 会话，网页远程输入 / 全双工托管终端。"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import anyio
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import broadcast, injector, promptlog, quota, sessions, transcripts
from .ptyman import PtyManager

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

TOKEN_FILE = DATA_DIR / "token.txt"
PORT = int(os.environ.get("CRW_PORT", "8765"))


def _load_token() -> str:
    env = os.environ.get("CRW_TOKEN")
    if env:
        return env
    if TOKEN_FILE.is_file():
        saved = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    token = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_token()
pty_manager = PtyManager()
app = FastAPI(title="Claude Session Bridge", docs_url=None, redoc_url=None)


def check_token(token: str | None, x_auth: str | None, cookie: str | None) -> bool:
    got = token or x_auth or cookie
    return bool(got) and secrets.compare_digest(got, TOKEN)


def require(token: str | None, x_auth_token: str | None,
            crw_token: str | None = None) -> None:
    if not check_token(token, x_auth_token, crw_token):
        raise HTTPException(status_code=401, detail="token 无效")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
def health(token: str | None = Query(None)):
    return {"ok": True, "authed": check_token(token, None, None)}


@app.get("/api/net")
def net(token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 9))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if not ip.startswith("169.254"):
            ips.add(ip)
    return {"port": PORT, "ips": sorted(ips)}


@app.get("/api/quota")
def get_quota(token: str | None = Query(None), x_auth_token: str | None = Header(None),
              force: bool = Query(False)):
    """代理本机 quota-monitor 套餐额度（同步阻塞，FastAPI 放线程池执行）。

    公网访问时远端浏览器够不到 127.0.0.1:8771，必须服务端转发；
    复用会话台 token，给无鉴权的额度服务挡一道。上游故障返回 200+ok=false。
    force=1 透传给上游绕过 540s 缓存（网页上的手动刷新按钮）。
    """
    require(token, x_auth_token)
    return quota.fetch_snapshot(force=force)


# ---------- CC KEY 切换 / 自定义 key 库 / cpolar 公网地址（均代理 quota-monitor） ----------
# 与会话台同 token 鉴权；上游 quota-monitor 只绑回环且无鉴权，这里挡一道。
# 动作结果是上游 switcher.status() 原文，前端拿它直接重绘 KEY 面板。

class _AutoBody(BaseModel):
    enabled: bool


class _ModeBody(BaseModel):
    key_id: str


class _AddKeyBody(BaseModel):
    label: str
    token: str


_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@app.post("/api/cc-switch/auto")
def cc_switch_auto(body: _AutoBody, token: str | None = Query(None),
                   x_auth_token: str | None = Header(None)):
    """开关自动切换（偏好由 quota-monitor 持久化）。"""
    require(token, x_auth_token)
    status, data = quota.proxy_action("/api/cc-switch/auto", "POST",
                                      {"enabled": body.enabled})
    return JSONResponse(data, status_code=status)


@app.post("/api/cc-switch/mode")
def cc_switch_mode(body: _ModeBody, token: str | None = Query(None),
                   x_auth_token: str | None = Header(None)):
    """手动把 cc key 切到 key 库中任意一把（只对新开 claude 会话生效）。"""
    require(token, x_auth_token)
    if not _KEY_ID_RE.match(body.key_id):
        raise HTTPException(status_code=400, detail="key_id 非法")
    status, data = quota.proxy_action("/api/cc-switch/mode", "POST",
                                      {"key_id": body.key_id})
    return JSONResponse(data, status_code=status)


@app.post("/api/cc-keys")
def add_cc_key(body: _AddKeyBody, token: str | None = Query(None),
               x_auth_token: str | None = Header(None)):
    """添加自定义 ark- key（仅手动切换目标，不监控用量）；格式校验上游为准。"""
    require(token, x_auth_token)
    if not body.label.strip() or len(body.label) > 40:
        raise HTTPException(status_code=400, detail="名称不能为空且不超过 40 字")
    if not body.token.strip() or len(body.token) > 200:
        raise HTTPException(status_code=400, detail="token 不能为空且不超过 200 字")
    status, data = quota.proxy_action(
        "/api/cc-keys", "POST", {"label": body.label, "token": body.token})
    return JSONResponse(data, status_code=status)


@app.delete("/api/cc-keys/{key_id}")
def delete_cc_key(key_id: str, token: str | None = Query(None),
                  x_auth_token: str | None = Header(None)):
    """删除自定义 key（内置主/备上游会拒）。"""
    require(token, x_auth_token)
    if not _KEY_ID_RE.match(key_id):
        raise HTTPException(status_code=400, detail="key_id 非法")
    status, data = quota.proxy_action(f"/api/cc-keys/{key_id}", "DELETE")
    return JSONResponse(data, status_code=status)


@app.get("/api/tunnels")
def list_tunnels(token: str | None = Query(None),
                 x_auth_token: str | None = Header(None)):
    """cpolar 隧道最新公网地址；上游挂了降级 200+ok=false（同 /api/quota 约定）。"""
    require(token, x_auth_token)
    status, data = quota.proxy_action("/api/tunnels")
    if status != 200:
        return {"tunnels": [], "ok": False,
                "error": data.get("detail", "额度服务不可用")}
    data.setdefault("tunnels", [])
    data["ok"] = True
    return data


def _procs_for_resolution(external: list[dict], managed: list[dict]) -> list[dict]:
    """构造排他解析输入。托管 PTY 里的 claude.exe 也会出现在进程枚举中
    （已标 managed），不能再作为外部会话参与，否则同 cwd 下它会抢走托管条目的文件。"""
    procs = [{"key": ("ext", s["pid"]), "cwd": s.get("cwd"),
              "started": s.get("started")}
             for s in external if not s["managed"]]
    procs += [{"key": ("mgr", m["id"]), "cwd": m.get("cwd"),
               "started": m.get("started")} for m in managed]
    return procs


def resolve_all() -> tuple[list[dict], list[dict], dict]:
    """枚举外部 + 托管会话，并全局排他解析每个会话当前对应的 jsonl。"""
    external = sessions.list_sessions(pty_manager.root_pids(),
                                      ttl=sessions.SNAP_TTL_SEC)
    managed = pty_manager.list()
    return external, managed, transcripts.resolve_files(
        _procs_for_resolution(external, managed))


def resolve_ext_pid(pid: int) -> tuple[dict | None, dict | None]:
    """只解析目标 pid 所在 cwd 组（2.5s 轮询热路径，不做全量枚举解析）。

    resolve_files 按 cwd 分组独立计算，单子组的结果与全量解析完全一致。
    返回 (会话 dict, 该 pid 的解析结果 {"file","title"}|None)。
    """
    external = sessions.list_sessions(pty_manager.root_pids(),
                                      ttl=sessions.SNAP_TTL_SEC)
    sess = next((s for s in external if s["pid"] == pid), None)
    if not sess or sess.get("managed"):
        return sess, None
    managed = pty_manager.list()
    same_cwd = sess.get("cwd")
    group = [{"key": ("ext", s["pid"]), "cwd": s.get("cwd"),
              "started": s.get("started")}
             for s in external if not s["managed"]
             and s.get("cwd") == same_cwd]
    group += [{"key": ("mgr", m["id"]), "cwd": m.get("cwd"),
               "started": m.get("started")}
              for m in managed if m.get("cwd") == same_cwd]
    resolved = transcripts.resolve_files(group)
    return sess, resolved.get(("ext", pid))





@app.get("/api/sessions")
def get_sessions(token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    external, managed, resolved = resolve_all()
    for s in external:
        r = resolved.get(("ext", s["pid"])) if not s["managed"] else None
        s["file"] = r["file"] if r else None
        s["title"] = r["title"] if r else None
    for m in managed:
        r = resolved.get(("mgr", m["id"])) or {"file": None, "title": None}
        m["file"] = r["file"]
        m["title"] = r["title"]
    return {"sessions": external, "managed": managed}


@app.get("/api/session/{pid}/probe")
def probe_session(pid: int, token: str | None = Query(None),
                  x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    if not sessions.session_by_pid(pid):
        raise HTTPException(404, "会话不存在（可能已退出）")
    return injector.probe(pid)


class InputBody(BaseModel):
    text: str
    submit: bool = True


@app.post("/api/session/{pid}/input")
def post_input(pid: int, body: InputBody,
               token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    sess = sessions.session_by_pid(pid)
    if not sess:
        raise HTTPException(404, "会话不存在（可能已退出）")
    r = injector.send_text(pid, body.text, body.submit)
    promptlog.log("single", f"pid {pid}", body.text, ok=bool(r.get("ok")),
                  error=r.get("error"), target_title=sess.get("title") or "",
                  cwd=sess.get("cwd") or "")
    return r


class _BroadcastBody(BaseModel):
    text: str
    submit: bool = True
    dry_run: bool = False   # 只枚举目标不注入（安全预览）


@app.post("/api/broadcast")
def post_broadcast(body: _BroadcastBody,
                   token: str | None = Query(None),
                   x_auth_token: str | None = Header(None)):
    """向所有会话（外部 + 托管）发同一条提示词，逐目标返回成败并落 MySQL。

    dry_run=true 只列出会被发送的目标，不注入（预览用）。
    """
    require(token, x_auth_token)
    if not body.text.strip():
        raise HTTPException(400, "文本为空")
    external = sessions.list_sessions(pty_manager.root_pids(),
                                      ttl=sessions.SNAP_TTL_SEC)
    managed = pty_manager.list()
    if body.dry_run:
        return {"dry_run": True,
                "targets": broadcast.enumerate_targets(external, managed)}
    batch_id = promptlog.new_batch_id()
    results = broadcast.run(body.text, body.submit, external, managed,
                            pty_manager.get, batch_id=batch_id)
    return {"batch_id": batch_id, "results": results,
            "ok_count": sum(1 for r in results if r["ok"]),
            "total": len(results)}


class KeyBody(BaseModel):
    key: str  # escape | ctrl_c | enter


@app.post("/api/session/{pid}/key")
def post_key(pid: int, body: KeyBody,
             token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    if not sessions.session_by_pid(pid):
        raise HTTPException(404, "会话不存在（可能已退出）")
    try:
        recs = injector.control_key(body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return injector.send_records(pid, recs)


@app.get("/api/transcript/files")
def transcript_files(pid: int, token: str | None = Query(None),
                     x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    sess, resolved = resolve_ext_pid(pid)
    if not sess:
        raise HTTPException(404, "会话不存在")
    files = transcripts.list_files(sess.get("cwd"))
    chosen = (resolved or {}).get("file")
    return {"cwd": sess.get("cwd"), "files": files, "chosen": chosen}


@app.get("/api/transcript")
def transcript(pid: int, file: str | None = None, after: int = 0,
               mode: str = Query(""),
               token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    sess, resolved = resolve_ext_pid(pid)
    if not sess:
        raise HTTPException(404, "会话不存在")
    if file is None:
        file = (resolved or {}).get("file")
    if not file:
        # 无可信关联（新会话尚未发言）：明确返回未找到，前端不得借用别人的记录
        return {"events": [], "size": after, "found": False}
    if mode == "user":  # 「只看我」：全量用户提示词，忽略 after
        return transcripts.read_user_events(sess.get("cwd"), file)
    return transcripts.read_events(sess.get("cwd"), file, after)


@app.get("/api/history")
def history(token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    """跨项目历史会话（扫盘）：已结束的微信桥任务 / 已退出的会话也能回看。"""
    require(token, x_auth_token)
    return {"items": transcripts.list_history()}


@app.get("/api/history/transcript")
def history_transcript(dir: str, file: str, after: int = 0,
                       mode: str = Query(""),
                       token: str | None = Query(None),
                       x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    if mode == "user":  # 「只看我」：全量用户提示词，忽略 after
        return transcripts.read_user_events_dir(dir, file)
    return transcripts.read_events_dir(dir, file, after)


@app.get("/api/dirs")
def dirs(token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    roots = [r"E:\1workspace", str(Path.home()), "E:\\", "C:\\"]
    out = []
    for root in roots:
        p = Path(root)
        if p.is_dir():
            try:
                subs = sorted((x for x in p.iterdir() if x.is_dir()),
                              key=lambda x: x.name.lower())
                out.append({"root": root, "dirs": [str(x) for x in subs][:100]})
            except OSError:
                continue
    return {"roots": out}


class ManagedBody(BaseModel):
    cwd: str


@app.post("/api/managed")
def create_managed(body: ManagedBody,
                   token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    cwd = Path(body.cwd).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        raise HTTPException(400, f"目录不存在: {body.cwd}")
    try:
        sess = pty_manager.start(str(cwd))
    except Exception as e:  # spawn 失败
        raise HTTPException(500, f"启动失败: {e}")
    return {"id": sess.id, "pid": sess.pid, "cwd": sess.cwd}


@app.delete("/api/managed/{sid}")
def delete_managed(sid: int, token: str | None = Query(None),
                   x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    if not pty_manager.stop(sid):
        raise HTTPException(404, "托管会话不存在")
    return {"ok": True}


@app.websocket("/ws/managed/{sid}")
async def ws_managed(ws: WebSocket, sid: int, token: str | None = Query(None)):
    if not check_token(token, None, None):
        await ws.close(code=4401)
        return
    sess = pty_manager.get(sid)
    if not sess or not sess.alive():
        await ws.close(code=4404)
        return
    await ws.accept()

    async def pump_out():
        while True:
            data = await anyio.to_thread.run_sync(sess.read)
            if data:
                await ws.send_text(data)
            else:
                await asyncio.sleep(0.03)

    async def pump_in():
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "input":
                sess.write(msg.get("data", ""))
            elif kind == "resize":
                sess.resize(int(msg.get("cols", 0)), int(msg.get("rows", 0)))

    try:
        await asyncio.gather(pump_out(), pump_in())
    except WebSocketDisconnect:
        pass  # 断开只是"摘机"，托管 claude 继续在本机跑


# ---------- 自察看门狗 ----------
# 进程活着但事件循环卡死时（2026-10-18 微信桥同款教训：进程级损坏只有换进程
# 能救），连续 WATCHDOG_MAX_FAILURES 次自探 /api/health 失败 → os._exit 自杀，
# 由自启 bat 的 30 次重试循环 ~20s 拉起新进程。调试用 CRW_NO_WATCHDOG=1 关闭。
WATCHDOG_INTERVAL = 30.0
WATCHDOG_MAX_FAILURES = 3


def self_health_ok(port: int, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def watchdog_loop(port: int, interval: float, max_failures: int,
                  exit_fn, sleep_fn=time.sleep, probe_fn=None) -> None:
    """连续 max_failures 次自探失败 → exit_fn(1)；成功一次即清零连败。"""
    probe = probe_fn or (lambda: self_health_ok(port))
    fails = 0
    while True:
        sleep_fn(interval)
        if probe():
            fails = 0
            continue
        fails += 1
        if fails >= max_failures:
            exit_fn(1)
            return


def start_watchdog(port: int) -> threading.Thread:
    t = threading.Thread(
        target=watchdog_loop,
        args=(port, WATCHDOG_INTERVAL, WATCHDOG_MAX_FAILURES, os._exit),
        daemon=True, name="crw-watchdog")
    t.start()
    return t


def _banner() -> None:
    print("=" * 62)
    print("  Claude 会话台 已启动")
    print(f"  本机:  http://127.0.0.1:{PORT}/?token={TOKEN}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 9))
        lan = s.getsockname()[0]
        s.close()
        print(f"  局域网: http://{lan}:{PORT}/?token={TOKEN}")
    except OSError:
        pass
    print("  (token 也保存在 data/token.txt；手机与电脑需同网段)")
    print("=" * 62, flush=True)


if __name__ == "__main__":
    import uvicorn
    _banner()
    if not os.environ.get("CRW_NO_WATCHDOG"):
        start_watchdog(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
