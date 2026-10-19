# -*- coding: utf-8 -*-
"""开机后自动播报会话台（claude-remote-web）公网地址。

流程：等 cpolar web8765 隧道建立（解析服务日志 NewTunnel 记录）
→ 等本地 8765 起来 → 探活公网地址 → 浏览器打开 → 微信「文件传输助手」播报。

- 免费套餐公网地址每次重启随机变化，只认脚本启动之后的新隧道记录，
  避免当天重启时捡到日志里的旧地址。
- 微信消息以 🌐 开头：wechat-claude-bridge 会忽略该前缀，不会误派 Claude 任务。
- 去重按（地址, 开机时间）计：本次开机已播报过同一地址且未带 --force 时退出
  （手工重跑不刷屏）；cpolar 免费版重启后地址可能不变，只按地址去重会漏报。
  记录在 data/last_public_url.txt（首行地址，次行开机时间 ISO）。
- 必须用 wechat-claude-bridge 的 .venv 运行（wxauto 装在里面）：
    E:\\1workspace\\wechat-claude-bridge\\.venv\\Scripts\\python.exe tools\\notify_public_url.py
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
LOG_DIR = Path.home() / ".cpolar" / "logs"
LAST_URL_FILE = DATA_DIR / "last_public_url.txt"

TUNNEL_NAME = "web8765"
LOCAL_PORT = 8765
WECHAT_WHO = "文件传输助手"

# 开机时尝试关闭的代理客户端：OPNANA（Electron 壳，C:\Program Files\OPNANA）
# + com.vortex.helper（7897 代理核心，是 OPNANA 的子进程）。
# 它开启系统代理但核心未就绪时 Chrome 全站打不开，播报页因此没内容。
VPN_PROCESS_NAMES = ("OPNANA.exe", "com.vortex.helper.exe")
VPN_PORT = 7897

_LINE_TS = re.compile(r'^time="([^"]+)"')
_URL_RE = re.compile(r'"Url":"(http://[^"]+)"')

log = logging.getLogger("notify")


# ---------- 日志解析（纯逻辑，可单测） ----------

def _line_ts(line: str) -> datetime | None:
    m = _LINE_TS.match(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def extract_tunnel_url(lines, since: datetime, tunnel: str = TUNNEL_NAME) -> str | None:
    """从 cpolar 服务日志行里取 tunnel 的 http 公网地址。

    只认 since 之后、Error 为空、Protocol 为 http 的 NewTunnel/RespStartTunnel
    记录；同日多条取最新（日志按时间顺序，后者覆盖前者）。
    """
    name_key = f'"TunnelName":"{tunnel}"'
    best: str | None = None
    for raw in lines:
        # 真实日志把 payload 嵌在 msg="..." 里，引号是转义的（\"Url\":\"http://...\"）
        line = raw.replace('\\"', '"')
        if name_key not in line or '"Protocol":"http"' not in line:
            continue
        if '"Error":""' not in line:
            continue
        ts = _line_ts(line)
        if ts is None or ts < since:
            continue
        m = _URL_RE.search(line)
        if m:
            best = m.group(1)
    return best


# ---------- 环境等待 ----------

def boot_time() -> datetime:
    """系统启动时间。cpolar 服务先于登录启动，隧道记录可能早于本脚本，
    以开机时间为下界既不错过本次开机的记录，也不会捡到上次开机的旧地址。"""
    try:
        import ctypes
        uptime_ms = ctypes.windll.kernel32.GetTickCount64()  # type: ignore[attr-defined]
        return datetime.now().astimezone() - timedelta(milliseconds=uptime_ms)
    except Exception:
        return datetime.now().astimezone() - timedelta(minutes=30)


_MAX_LOG_FILES = 4  # 跨午夜轮转后隧道记录在最近几个文件之一（与 quota-monitor 一致）


def _log_files() -> list[Path]:
    """cpolar_service.log* 按 mtime 从新到旧。午夜轮转后活动文件是空的，
    本次开机的隧道记录落在刚轮转走的文件里，必须回看多个文件。"""
    files = [p for p in LOG_DIR.glob("cpolar_service.log*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:_MAX_LOG_FILES]


def wait_tunnel_url(since: datetime, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in _log_files():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            url = extract_tunnel_url(lines, since)
            if url:
                return url
        time.sleep(3)
    return None


def wait_local_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _disable_system_proxy() -> None:
    """关掉 Windows 系统代理并广播设置变更。

    强杀代理客户端不会还原注册表，Chrome 会继续指向死代理（全站打不开），
    必须一并清掉 ProxyEnable；广播 WM_SETTINGCHANGE 让运行中的浏览器立即感知。
    """
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    except OSError as exc:
        log.warning("关闭系统代理失败: %s", exc)
        return
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Internet Settings",
            SMTO_ABORTIFHUNG, 1000, None)
    except Exception:
        pass  # 广播失败不影响，注册表已改，新开的浏览器必然直连


def ensure_vpn_off(attempts: int = 3, interval: float = 5.0) -> bool:
    """开机时尝试关闭 OPNANA 代理（7897），返回 7897 是否已不在监听。

    非致命：关不掉仅告警，不阻断播报。循环重试是为了覆盖
    「OPNANA 自启比本脚本晚」的开机竞态。
    """
    for attempt in range(1, attempts + 1):
        if not _port_listening(VPN_PORT):
            _disable_system_proxy()  # 核心没起也要兜底清系统代理
            return True
        log.info("检测到代理 %d 在监听，尝试关闭 OPNANA（第 %d/%d 次）",
                 VPN_PORT, attempt, attempts)
        for name in VPN_PROCESS_NAMES:
            # 进程不存在时 taskkill 报错退出，属正常情况，无需检查
            subprocess.run(["taskkill", "/IM", name, "/T", "/F"],
                           capture_output=True)
        _disable_system_proxy()
        time.sleep(interval)
    if _port_listening(VPN_PORT):
        log.warning("OPNANA 仍未关闭（%d 监听中），继续播报", VPN_PORT)
        return False
    return True


# 探活必须直连：系统代理（127.0.0.1:7897）开机时往往还没起，urllib 默认走
# getproxies() 读到的系统代理会直接 ConnectionRefused，cpolar 是国内服务不需要代理。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_public(url: str, token: str, timeout: float) -> bool:
    """公网地址探活（HTTP GET 带 token，期望 200，直连不走系统代理）。"""
    deadline = time.time() + timeout
    target = f"{url}/?token={token}"
    while time.time() < deadline:
        try:
            with _DIRECT_OPENER.open(target, timeout=10) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:
            log.debug("探活未通过: %s", exc)
        time.sleep(5)
    return False


# ---------- 微信 ----------

def send_wechat(text: str, retries: int = 12, interval: float = 20.0) -> bool:
    """经 wxauto 发到文件传输助手；开机时微信可能还没登录，做重试。"""
    from wxauto import WeChat  # 延迟导入：--no-wechat / 单测时不依赖 wxauto

    for attempt in range(1, retries + 1):
        try:
            wx = WeChat()
            wx.SendMsg(text, who=WECHAT_WHO)
            return True
        except Exception as exc:
            log.warning("微信发送失败（第 %d/%d 次）: %s", attempt, retries, exc)
            time.sleep(interval)
    return False


_BOOT_TOLERANCE_S = 120  # GetTickCount64 推算的开机时间在多次运行间有秒级漂移


def _already_notified_this_boot(url: str) -> bool:
    """本次开机是否已播报过该地址。

    last_public_url.txt 首行为地址、次行为播报时推算的开机时间；
    旧格式（仅一行地址）或开机时间对不上都视为未播报。
    """
    try:
        parts = LAST_URL_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if len(parts) < 2 or parts[0].strip() != url:
        return False
    try:
        recorded_boot = datetime.fromisoformat(parts[1].strip())
    except ValueError:
        return False
    return abs((recorded_boot - boot_time()).total_seconds()) < _BOOT_TOLERANCE_S


def _record_notified(url: str) -> None:
    LAST_URL_FILE.write_text(f"{url}\n{boot_time().isoformat()}\n", encoding="utf-8")


# ---------- 主流程 ----------

def read_token() -> str:
    env = os.environ.get("CRW_TOKEN", "").strip()
    if env:
        return env
    token_file = DATA_DIR / "token.txt"
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def build_message(url: str, token: str, probe_ok: bool) -> str:
    lines = [
        "🌐 会话台公网地址",
        f"{url}/?token={token}",
        "（免费版每次开机随机更换；本机浏览器请用 HTTP，HTTPS 会被重置）",
    ]
    if not probe_ok:
        lines.append("⚠️ 播报前公网探活未通过，服务可能仍在启动中，稍后再试")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="播报会话台公网地址")
    parser.add_argument("--force", action="store_true", help="地址未变也重新播报")
    parser.add_argument("--no-browser", action="store_true", help="不打开浏览器")
    parser.add_argument("--no-wechat", action="store_true", help="不发微信")
    parser.add_argument("--keep-vpn", action="store_true",
                        help="不关闭 OPNANA 代理（手工重跑且代理正在用时加这个）")
    parser.add_argument("--tunnel-timeout", type=float, default=300.0)
    parser.add_argument("--probe-timeout", type=float, default=240.0,
                        help="公网探活最长等待秒数（等 cpolar 隧道/本地服务就绪）；"
                             "探活通过才开浏览器，否则打开的是 cpolar 错误页")
    parser.add_argument("--since-minutes", type=float, default=None,
                        help="接受最近 N 分钟内的隧道记录（默认以本次开机时间为下界）")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(DATA_DIR / "notify-public-url.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    if not args.keep_vpn:
        # 关机时 OPNANA 常不还原系统代理：开机后注册表还指着没人监听的 7897，
        # Chrome 全站打不开。无论端口是否监听都要清一次系统代理。
        ensure_vpn_off()

    if args.since_minutes:
        since = datetime.now().astimezone() - timedelta(minutes=args.since_minutes)
    else:
        since = boot_time() - timedelta(seconds=60)
    log.info("等待 cpolar 隧道 %s（下界 %s）...", TUNNEL_NAME,
             since.strftime("%H:%M:%S"))
    url = wait_tunnel_url(since, args.tunnel_timeout)
    if not url:
        log.error("超时未等到隧道地址")
        return 1
    log.info("隧道地址: %s", url)

    if not args.force and _already_notified_this_boot(url):
        log.info("本次开机已播报过该地址，退出（--force 可强制重播）")
        return 0

    token = read_token()
    if not wait_local_port(LOCAL_PORT, timeout=120):
        log.warning("本地 %d 端口未就绪，继续播报", LOCAL_PORT)

    probe_ok = probe_public(url, token, timeout=args.probe_timeout)
    log.info("公网探活: %s", "OK" if probe_ok else "未通过")

    if not args.no_browser:
        if not args.keep_vpn:
            ensure_vpn_off()  # 兜底：等待隧道期间 OPNANA 可能自启并重设系统代理
        if not probe_ok:
            # 浏览器是主渠道必须打开，但此时打开大概率是 cpolar 错误页，
            # 需要手动刷新；正常路径是探活通过后打开即有内容。
            log.warning("探活未通过仍打开浏览器，页面可能需手动刷新")
        os.startfile(f"{url}/?token={token}")  # noqa: S606 本机自用脚本
        log.info("已在浏览器打开")

    if not args.no_wechat:
        # 微信只是补充渠道（开机时微信常还没登录），失败不影响主流程：
        # 上面已在本地浏览器打开带 token 的地址，可自行复制。
        if send_wechat(build_message(url, token, probe_ok)):
            log.info("已发微信「%s」", WECHAT_WHO)
        else:
            log.warning("微信播报失败（不影响，本地浏览器已打开地址）")

    _record_notified(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
