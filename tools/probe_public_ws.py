# -*- coding: utf-8 -*-
"""经 cpolar 公网隧道验证 WSS：连接托管会话，发 resize 触发 TUI 重绘，
确认能收到 claude 屏幕帧。用法: python probe_public_ws.py <sid> <wss_base>"""
import asyncio
import json
import sys
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (ROOT / "data" / "token.txt").read_text(encoding="utf-8").strip()
SID = sys.argv[1]
BASE = sys.argv[2].rstrip("/").replace("https://", "wss://").replace("http://", "ws://")


async def main() -> None:
    uri = f"{BASE}/ws/managed/{SID}?token={TOKEN}"
    frames: list[str] = []
    async with websockets.connect(uri, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 30}))
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                frames.append(msg)
                total = sum(len(f) for f in frames)
                if total > 300:
                    break
        except asyncio.TimeoutError:
            pass
    joined = "".join(frames)
    print(json.dumps({
        "frames": len(frames),
        "bytes": len(joined),
        "has_ansi": "\x1b[" in joined,
        "claude_screen": "Claude" in joined or "❯" in joined,
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
