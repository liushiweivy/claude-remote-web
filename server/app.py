# -*- coding: utf-8 -*-
"""Claude 会话台：查看本机全部 claude 会话，网页远程输入 / 全双工托管终端。"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
from pathlib import Path

import anyio
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import injector, sessions, transcripts
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


@app.get("/api/sessions")
def get_sessions(token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    external = sessions.list_sessions(pty_manager.root_pids())
    for s in external:
        s["title"] = transcripts.session_title(s.get("cwd"), s.get("started"))
    managed = pty_manager.list()
    for m in managed:
        m["title"] = transcripts.session_title(m.get("cwd"), m.get("started"))
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
    if not sessions.session_by_pid(pid):
        raise HTTPException(404, "会话不存在（可能已退出）")
    return injector.send_text(pid, body.text, body.submit)


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
    sess = sessions.session_by_pid(pid)
    if not sess:
        raise HTTPException(404, "会话不存在")
    files = transcripts.list_files(sess.get("cwd"))
    chosen = transcripts.choose_file(sess.get("cwd"), sess.get("started"))
    return {"cwd": sess.get("cwd"), "files": files, "chosen": chosen}


@app.get("/api/transcript")
def transcript(pid: int, file: str | None = None, after: int = 0,
               token: str | None = Query(None), x_auth_token: str | None = Header(None)):
    require(token, x_auth_token)
    sess = sessions.session_by_pid(pid)
    if not sess:
        raise HTTPException(404, "会话不存在")
    file = file or transcripts.choose_file(sess.get("cwd"), sess.get("started"))
    return transcripts.read_events(sess.get("cwd"), file, after)


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
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
