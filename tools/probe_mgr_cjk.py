# -*- coding: utf-8 -*-
"""托管会话中文全链路探针：直连 WS，向指定托管会话注入中文消息，
收集回显与模型回复，检查是否有 CJK 叠字。仅用于本机自测。

用法: python tools/probe_mgr_cjk.py <sid>   （sid 必须显式传入）
结果写 tools/probe_out_<sid>.txt（UTF-8，WS 原始帧以 repr 留存）。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (ROOT / "data" / "token.txt").read_text(encoding="utf-8").strip()
if len(sys.argv) < 2 or not sys.argv[1].isdigit():
    print("usage: python tools/probe_mgr_cjk.py <numeric sid>", file=sys.stderr)
    sys.exit(2)
SID = sys.argv[1]
PROMPT = "只回复 ZQ_MGR_OK 这一行，不要输出任何其他内容"
OUT = ROOT / "tools" / f"probe_out_{SID}.txt"


async def main() -> None:
    uri = f"ws://127.0.0.1:8765/ws/managed/{SID}?token={TOKEN}"
    log = OUT.open("w", encoding="utf-8")

    def logline(s: str) -> None:
        log.write(s + "\n")
        log.flush()

    async with websockets.connect(uri) as ws:
        logline(f"[{time.strftime('%H:%M:%S')}] connected sid={SID}")

        async def reader(deadline: float, tag: str) -> str:
            buf = []
            while time.monotonic() < deadline:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=min(left, 2.0))
                except asyncio.TimeoutError:
                    continue
                buf.append(msg)
                logline(f"[{tag}] frame {msg!r}")
            return "".join(buf)

        # 1) 先收 2.5s 初始屏幕，确认 TUI 已在
        boot = await reader(time.monotonic() + 2.5, "BOOT")
        logline(f"boot_chars={len(boot)}")

        # 2) 发中文提示词 + 回车
        await ws.send(json.dumps({"type": "input", "data": PROMPT}))
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "input", "data": "\r"}))
        logline("sent CJK prompt + enter")

        # 3) 最长收 90s 回复；看到 ZQ_MGR_OK 提前结束
        end = time.monotonic() + 90
        allbuf = ""
        while time.monotonic() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            allbuf += msg
            logline(f"[RECV] {msg!r}")
            if "ZQ_MGR_OK" in allbuf.replace(" ", ""):
                # 再多收 2s 拿收尾帧
                await reader(time.monotonic() + 2.0, "TAIL")
                break

        joined = boot + allbuf
        # 叠字启发式：同一中文字连续重复 >=3（TUI 重绘通常不会这样）
        cjk_runs = {}
        run_ch = ""
        run_n = 0
        for ch in joined:
            if ch == run_ch and ord(ch) > 0x2E80:
                run_n += 1
            else:
                if run_n >= 3:
                    cjk_runs[run_ch] = run_n
                run_ch, run_n = ch, 1
        if run_n >= 3:
            cjk_runs[run_ch] = run_n

        got = "ZQ_MGR_OK" in allbuf.replace(" ", "")
        # 检查我们输入的中文是否原样回显（非双份）
        echo_ok = "只回复" in joined
        result = {
            "got_reply": got,
            "cjk_echo_ok": echo_ok,
            "cjk_triple_runs": cjk_runs,
            "total_chars": len(joined),
        }
        logline("RESULT " + json.dumps(result, ensure_ascii=False))
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
