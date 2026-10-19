# -*- coding: utf-8 -*-
"""广播：把一条提示词发给所有会话（外部注入 + 托管 PTY），逐目标落 MySQL。

- 外部会话（非托管标记）走 injector.send_text（CONIN$ 注入，中文可能叠字——平台限制）
- 托管会话走 PTY 写入通道（中文正常）；多行文本用 bracketed-paste 防 TUI 逐行提交
- 每个目标单独记录成败；任何单点失败不影响其他目标
"""
from __future__ import annotations

from . import injector, promptlog


def build_pty_payload(text: str, submit: bool = True) -> str:
    """PTY 写入通道的字符串载荷：多行走 bracketed-paste（与 injector 同语义）。"""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    payload = "\x1b[200~" + body + "\x1b[201~" if "\n" in body else body
    if submit:
        payload += "\r"
    return payload


def enumerate_targets(external: list[dict], managed: list[dict]) -> list[dict]:
    """广播目标 = 非托管的外部会话 + 全部托管会话。

    托管 PTY 里的 claude.exe 也出现在进程枚举中（带 managed 标记），
    必须跳过作为外部目标——否则同一会话被发两遍，且 CONIN$ 路径中文叠字。
    """
    targets = []
    for s in external:
        if s.get("managed"):
            continue
        targets.append({"kind": "ext", "pid": s["pid"],
                        "title": s.get("title") or "", "cwd": s.get("cwd") or ""})
    for m in managed:
        targets.append({"kind": "mgr", "id": m["id"], "pid": m.get("pid"),
                        "title": m.get("title") or "", "cwd": m.get("cwd") or ""})
    return targets


def _label(t: dict) -> str:
    return f"pid {t['pid']}" if t["kind"] == "ext" else f"mgr {t['id']}"


def run(text: str, submit: bool, external: list[dict], managed: list[dict],
        mgr_get, batch_id: str = "") -> list[dict]:
    """逐目标发送 + 落库；返回每目标结果列表（顺序与 enumerate_targets 一致）。"""
    results = []
    for t in enumerate_targets(external, managed):
        label = _label(t)
        try:
            if t["kind"] == "ext":
                r = injector.send_text(t["pid"], text, submit)
            else:
                sess = mgr_get(t["id"])
                if sess is None or not sess.alive():
                    r = {"ok": False, "error": "托管会话已退出"}
                else:
                    sess.write(build_pty_payload(text, submit))
                    r = {"ok": True}
        except Exception as e:  # noqa: BLE001 - 单点失败不影响其他目标
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        promptlog.log("broadcast", label, text, ok=bool(r.get("ok")),
                      error=r.get("error"), batch_id=batch_id,
                      target_title=t.get("title", ""), cwd=t.get("cwd", ""))
        results.append({**t, "ok": bool(r.get("ok")), "error": r.get("error")})
    return results
