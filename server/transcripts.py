# -*- coding: utf-8 -*-
"""读取 ~/.claude/projects 下的会话 jsonl，给外部会话提供"输出镜像"。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .anchors import MATCH_WINDOW_SEC, text_matches

PROJECTS_DIR = Path.home() / ".claude" / "projects"
TAIL_BYTES = 128 * 1024
MAX_TAIL_BYTES = 4 * 1024 * 1024  # 最后一行可能是数百 KB 的巨型 JSON（工具结果）
PREVIEW_LIMIT = 400
PREVIEW_TITLE_MAX = 28
HEAD_SCAN_CAP = 512 * 1024  # 新版元数据头很长：实测 ai-title 在 300KB+，首条消息在 100KB+

# 进程↔文件时间匹配参数
START_TOLERANCE_SEC = 2.0       # 文件首事件允许比进程创建早的时钟噪声
MAX_LOOKBACK_GAP_SEC = 1800.0   # 老进程 /clear 开新会话：首事件最晚晚于启动 30 分钟

# jsonl 文件名是 uuid，禁止任何路径分隔/父目录跳转（file 参数来自网络，防越权读取）
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.jsonl$")
# 历史视图的目录参数是 encode_cwd 产物（只含安全字符），同样拒绝任何路径分隔
SAFE_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 微信桥每任务落一行 DONE ... session=<id>，凭 id 集合给历史会话打「微信桥」来源
BRIDGE_SESSIONS_LOG = Path(
    os.environ.get("CRW_BRIDGE_SESSIONS_LOG",
                   r"E:\1workspace\wechat-claude-bridge\logs\sessions.log"))
_BRIDGE_SID_RE = re.compile(r"\bsession=([0-9a-fA-F-]{8,})\b")

HISTORY_LIMIT = 30
_history_cache: dict = {"ts": 0.0, "fp": (), "items": []}
HISTORY_CACHE_TTL = 15.0

# 会话列表每 5s 轮询，按 cwd 缓存目录扫描结果，避免反复 glob/stat/读文件头
# 值: (monotonic, files, fingerprint)；fingerprint 变化即失效（新文件/续写秒感知）
_file_cache: dict[str, tuple[float, list[dict], tuple]] = {}
_file_cache_lock = threading.Lock()
FILE_CACHE_TTL = 20.0
# 单文件头部解析缓存：{cwd_key: {filename: ((mtime_ns, size), title, preview, first_ts_iso)}}
# 活跃会话爆发写入时指纹会让目录级缓存失效，但未变化的绝大多数文件不必重读头部。
_meta_cache: dict[str, dict[str, tuple]] = {}

# 锚点冻结认领缓存：{Anchor: fname | None}。匹配窗关闭（now >= ts+20s）后，
# 窗内事件早已落盘，认领决定不会再变 —— 冻结后每个轮询直接套用，不再整扫
# 活跃大文件（实测 8.2MB 文件单次整扫 70-100ms，每 2.5s 一次）。None 是负缓存：
# 窗内确实没有匹配事件，后续也不会变。锚点过期即随提交清理；按 Anchor 值命中，
# 属主进程退出时该锚点在组内被 live-key 过滤，缓存自然不生效。
_claim_cache: dict = {}
_claim_cache_lock = threading.Lock()
CLAIM_CACHE_MAX = 2048
# 负冻结宽限：首个关窗轮询落在 [ts+20, ts+22.5]，窗内最后 ~0.5s
# 的事件可能正在刷盘；None 多等 5s 再冻结，正认领立即冻结
NEGATIVE_FREEZE_GRACE_SEC = 5.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_frozen(anchor_list: list, now: datetime) -> dict[int, str | None]:
    """命中冻结缓存的锚点 → {anchor_list 下标: fname 或 None}。"""
    with _claim_cache_lock:
        return {i: _claim_cache[a] for i, a in enumerate(anchor_list)
                if a in _claim_cache}


def _commit_frozen(new: dict, now: datetime) -> None:
    """把关窗锚点本轮的决定（含 None 负缓存）写入冻结表，顺手剪过期/限容。"""
    if not new:
        return
    with _claim_cache_lock:
        for a, verdict in new.items():
            # 正认领粘性：并发 resolve 时迟到的陈旧负扫描（事件刷盘前扫的）
            # 不得把先到的正认领覆盖成 None；None→正的升级允许
            if verdict is None and _claim_cache.get(a) is not None:
                continue
            _claim_cache[a] = verdict
        for a in [a for a in _claim_cache if a.expires_at <= now]:
            del _claim_cache[a]
        if len(_claim_cache) > CLAIM_CACHE_MAX:  # 极端长稳跑兜底：留最新的
            keep = set(sorted(_claim_cache, key=lambda a: a.ts,
                              reverse=True)[:CLAIM_CACHE_MAX])
            for a in list(_claim_cache):
                if a not in keep:
                    del _claim_cache[a]


def _clear_claim_cache() -> None:
    with _claim_cache_lock:
        _claim_cache.clear()


def encode_cwd(cwd: str) -> str:
    """C:\\Users\\4v -> C--Users-4v（Claude Code 目录编码规则）。"""
    return re.sub(r"[:/\\]", "-", cwd)


def project_dir(cwd: str | None) -> Path | None:
    if not cwd:
        return None
    d = PROJECTS_DIR / encode_cwd(cwd)
    return d if d.is_dir() else None


def _parse_ts(ts: str) -> datetime:
    """解析 jsonl 时间戳，始终返回 timezone-aware UTC。

    jsonl 是外部数据边界：第三方工具/格式变体可能写出无时区时间，
    naive datetime 与 aware datetime 相减会抛 TypeError 并让会话 API 全挂。
    """
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _scan_head(path: Path) -> tuple[str | None, str | None, datetime | None]:
    """一次扫描文件头部，同时取 (ai-title, 首条用户消息预览, 第一条带 ts 的时间)。

    新版 jsonl 头部有大量无 timestamp 元数据行（last-prompt/mode/
    permission-mode/atis-latch…），旧版分别用 8KB/64KB 窗口各读一遍文件，
    既慢又频繁拿到 None（实测 ai-title 在 300KB+）。合并为一次读取。
    """
    with path.open("rb") as f:
        head = f.read(HEAD_SCAN_CAP)
    title: str | None = None
    preview: str | None = None
    first_ts: datetime | None = None
    for ln in head.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if first_ts is None and d.get("timestamp"):
            try:
                first_ts = _parse_ts(d["timestamp"])
            except ValueError:
                pass  # 时间戳格式异常的行直接忽略，不影响其它字段
        if title is None and d.get("type") == "ai-title" \
                and isinstance(d.get("title"), str):
            title = d["title"]
        if preview is None:
            ev = parse_line(ln)
            if ev and ev.get("role") == "user" and ev.get("kind") == "text":
                text = " ".join(ev["text"].split())
                if text:
                    preview = text[:PREVIEW_TITLE_MAX] + \
                        ("…" if len(text) > PREVIEW_TITLE_MAX else "")
        if title is not None and preview is not None and first_ts is not None:
            break
    return title, preview, first_ts


def list_files(cwd: str | None) -> list[dict]:
    d = project_dir(cwd)
    if not d:
        return []
    key = cwd or ""
    with _file_cache_lock:
        meta = _meta_cache.setdefault(key, {})
    out = []
    seen: set[str] = set()
    for f in d.glob("*.jsonl"):
        st = f.stat()
        sig = (st.st_mtime_ns, st.st_size)
        seen.add(f.name)
        with _file_cache_lock:
            cached = meta.get(f.name)
        if cached and cached[0] == sig:
            _sig, title, preview, first_ts_iso = cached
        else:
            title, preview, first_ts = _scan_head(f)
            first_ts_iso = first_ts.isoformat() if first_ts else None
            with _file_cache_lock:
                meta[f.name] = (sig, title, preview, first_ts_iso)
        out.append({
            "file": f.name,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "size": st.st_size,
            "first_ts": first_ts_iso,
            "title": title,
            "preview": preview,
        })
    with _file_cache_lock:  # 清掉已删除文件的陈旧缓存项
        for dead in set(meta) - seen:
            del meta[dead]
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _fingerprint(cwd: str | None) -> tuple:
    """目录的廉价指纹：文件名/mtime_ns/尺寸。只 glob+stat，不读内容。"""
    d = project_dir(cwd)
    if not d:
        return ()
    try:
        return tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size)
                            for f in d.glob("*.jsonl")))
    except OSError:
        return ()


def list_files_cached(cwd: str | None, ttl: float = FILE_CACHE_TTL) -> list[dict]:
    """list_files 的短 TTL 缓存（会话列表轮询用）。

    TTL 内若目录指纹变化（新会话落盘/文件续写），立即失效重扫，
    否则新会话最长要等一个 TTL 才能关联上。指纹是目录 I/O，放锁外计算，
    避免所有 API 线程被串行在 stat 后面。
    """
    key = cwd or ""
    now = time.monotonic()
    fp = _fingerprint(cwd)
    with _file_cache_lock:
        hit = _file_cache.get(key)
        if hit and now - hit[0] < ttl and hit[2] == fp:
            return hit[1]
    files = list_files(cwd)
    with _file_cache_lock:
        _file_cache[key] = (now, files, fp)
    return files


def _file_title(f: dict) -> str | None:
    return f.get("title") or f.get("preview")


def resolve_files(procs: list[dict], anchors=None) -> dict:
    """进程 ↔ jsonl 全局排他解析（同 cwd 多会话防串台的核心）。

    procs: [{"key": 任意可哈希标识, "cwd": str|None, "started": iso 字符串}]
    anchors: anchors.AnchorStore|None —— 有注入锚点时先做 phase-0 硬认领。
    返回 {key: {"file": 文件名|None, "title": str|None}}。
    没有可信证据时 file 为 None —— 调用方必须诚实显示"等待首条消息"，
    绝不允许借用别的会话文件（旧 choose_file 的 files[0] 兜底即串台根因）。

    phase-0（注入实证，见 _phase0_claims）：服务端自己注入的文本在 20s 内
    落盘到哪个文件，文件就硬归注入目标，可推翻两轮时间匹配的错配猜测；
    锚点只对本组仍存活的属主生效，关窗后决定冻结，不反复整扫文件。
    两轮都走"最大化被服务进程数"的排他匹配（见 _assign_exclusive）：
    第一轮（创建证据）：文件首事件落在 [启动-2s, 启动+30min] 窗口内即有边，
    1:1 主匹配保证每个活进程优先各得一个文件 —— 晚启动的进程即使 gap 略大，
    也不会被更近的进程把两个文件都赢走（实测：两进程间隔 2.5 分钟，两个文件
    间隔 4 秒，逐文件 argmin 会让同一进程赢走两个、另一个会话饿死/串台）。
    主匹配没分出去的文件再按最小 gap 附加给最接近的进程（/clear 开新
    session 的旧文件），进程"当前文件"取首事件最晚者。
    第二轮（续写证据，resume/无时间戳头）：第一轮无文件的进程，对"启动后
    被写过、且第一轮无人拥有"的旧文件按 mtime gap 做同样的排他匹配，
    gap 同样封顶在 MAX_LOOKBACK_GAP_SEC（30min）—— 超时宁可诚实返回 None，
    也不许闲置进程无限回溯偷走老进程里新开的会话（2026-10-21 串台路径二），
    注入场景由 phase-0 锚点覆盖。同一进程拿到多个时以 mtime 最新者为当前文件。
    """
    result = {p["key"]: {"file": None, "title": None} for p in procs}
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in procs:
        groups[p.get("cwd") or ""].append(p)
    for cwd, plist in groups.items():
        _resolve_group(cwd, plist, result, anchor_store=anchors)
    return result


# 匹配矩阵的哨兵代价：OPT_OUT = 文件主动放弃匹配（虚拟进程列），
# INF = 该 (文件,进程) 组合不可达。OPT_OUT 必须大于任何真实代价总和。
_OPT_OUT = 1.0e7
_INF = 1.0e9


def _assign_exclusive(costs: list[list[float | None]]) -> dict[int, int]:
    """文件→进程的 1:1 最优主匹配，优化目标按字典序：

    1. 匹配数最多（最大化被服务进程数 —— 防"一个进程赢走全部文件"）；
    2. 最大单边代价最小（minimax：真实案例里两条配对总 gap 相同，
       minimax 唯一选中"各回各家"的人类真相）；
    3. 总代价最小（匈牙利算法）。

    costs: F 行（文件，已按时间排序）× P 列（进程，已按启动排序），
    None = 窗口外不可达。返回 {文件行号: 进程列号}，未匹配的行不在 dict 中。
    组规模天然很小：只有首事件落在活进程启动窗口（约 30 分钟）内的文件
    才有边，通常 F/P 都在个位数。
    """
    fcnt = len(costs)
    pcnt = fcnt and len(costs[0])
    if not fcnt or not pcnt:
        return {}
    edges = [(costs[i][j], i, j) for i in range(fcnt) for j in range(pcnt)
             if costs[i][j] is not None]
    if not edges:
        return {}

    def cardinality(limit: float) -> int:
        """只许 gap <= limit 的边时的最大匹配数（Kuhn 增广路）。"""
        adj: dict[int, list[int]] = defaultdict(list)
        for c, i, j in edges:
            if c <= limit:
                adj[i].append(j)
        match_col: dict[int, int] = {}

        def augment(row: int, seen: set[int]) -> bool:
            for col in adj[row]:
                if col in seen:
                    continue
                seen.add(col)
                if col not in match_col or augment(match_col[col], seen):
                    match_col[col] = row
                    return True
            return False

        return sum(1 for i in range(fcnt) if augment(i, set()))

    max_card = cardinality(float("inf"))
    # 最小可行 minimax 阈值：让最大匹配数达到 max_card 的最低边价
    threshold = float("inf")
    for limit in sorted({c for c, _i, _j in edges}):
        if cardinality(limit) == max_card:
            threshold = limit
            break

    # 匈牙利最小权匹配：F 行 × (P 真实列 + F 虚拟放弃列)，n <= m
    m = pcnt + fcnt
    matrix = [[0.0] * m for _ in range(fcnt)]
    for i in range(fcnt):
        for j in range(pcnt):
            c = costs[i][j]
            matrix[i][j] = c if (c is not None and c <= threshold) else _INF
        for j in range(pcnt, m):
            matrix[i][j] = _OPT_OUT  # 放弃匹配：保证不会为了小 gap 饿死别的进程
    return _hungarian(matrix, pcnt)


def _hungarian(matrix: list[list[float]], real_cols: int) -> dict[int, int]:
    """方阵补平的匈牙利最小权匹配（n 行 <= m 列），只返回落到真实列的匹配。"""
    n = len(matrix)
    m = len(matrix[0])
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        minv = [float("inf")] * (m + 1)
        used = [False] * (m + 1)
        j0 = 0
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = matrix[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    out: dict[int, int] = {}
    for j in range(1, m + 1):
        if p[j] != 0 and j - 1 < real_cols:
            out[p[j] - 1] = j - 1
    return out


def _attach_remaining(cands: list[tuple[int, float, int]], primary: dict[int, int],
                      ) -> dict[int, int]:
    """主匹配没分出去的文件，按最小 gap 附加给最接近的进程（/clear 旧文件归属）。

    返回 {文件行: 进程列}，只含未被主匹配占用的文件，每个文件至多一个属主。
    """
    taken = set(primary)
    best: dict[int, tuple[float, int]] = {}
    for i, c, j in cands:
        if i in primary:
            continue
        if i not in best or c < best[i][0]:
            best[i] = (c, j)
    return {i: j for i, (c, j) in best.items() if i not in taken}


def _phase0_claims(cwd: str | None, anchor_list: list, files: list[dict],
                   now: datetime | None = None):
    """注入锚点 phase-0 硬认领（2026-10-21 二代串台修复）。

    服务端精确知道注入时刻/目标/文本，落盘用户事件在 MATCH_WINDOW_SEC 内
    带着一致文本出现，是比时间启发式强得多的归属证据：

    - 追写锚点：锚点文本在窗内出现在某文件的用户事件里 → 硬认领该文件，
      即使两轮时间匹配此前把它错配给了别的进程（文件直接移出后续匹配池）。
    - 出生锚点：窗内只有一个新文件（first_ts 落在注入时刻附近），且其首条
      用户文本 squeeze 后对得上才认 —— 既覆盖 CONIN$ 中文逐字叠写
      （libuv 双消费），又防止把同刻本地打字的另一进程首文件错认给锚点。
    - 多锚点按 |事件-锚点| 时间差升序贪心，跨属主排他（同属主可重复认领
      同一文件：多条注入进同一会话）；一个锚点至多认领一个文件。
    - 匹配窗关闭后的锚点决定被冻结（_claim_cache，含负结果）：窗内事件
      早已落盘，结论不变，后续轮询零整扫。

    anchor_list 只含本组存活属主的锚点（调用方按 live key 过滤）。
    返回 (claimed_names:set[str], owned:{key: [(first_ts, file_dict)]})。
    """
    now = now or _now_utc()
    by_name = {f["file"]: f for f in files}
    first_dt = {f["file"]: (datetime.fromisoformat(f["first_ts"])
                            if f["first_ts"] else None) for f in files}
    mtime_dt = {f["file"]: datetime.fromisoformat(f["mtime"]) for f in files}
    user_events: dict[str, list] = {}

    def events_for(fname: str) -> list:
        if fname not in user_events:  # read_user_events 按文件签名缓存
            user_events[fname] = read_user_events(cwd, fname)["events"]
        return user_events[fname]

    claimed: dict[str, object] = {}
    win: dict[int, str] = {}  # 已决锚点 ai -> 认领文件名（冻结命中或本轮新判）

    # 1) 冻结锚点：按注入时间升序直接套用旧决定；文件已删/属主撞车则跳过
    frozen = _snapshot_frozen(anchor_list, now)
    for ai in sorted(frozen):
        fname = frozen[ai]
        if fname is None or fname not in by_name:
            continue
        anchor = anchor_list[ai]
        owner = claimed.get(fname)
        if owner is not None and owner != anchor.key:
            continue
        claimed[fname] = anchor.key
        win[ai] = fname

    # 2) 未冻结锚点：mtime 预筛 + 整扫文本边（窗内事件）
    text_edges: list[tuple[float, int, object, str]] = []
    for ai, anchor in enumerate(anchor_list):
        if ai in frozen:
            continue
        lo = anchor.ts - timedelta(seconds=MATCH_WINDOW_SEC)
        for f in files:
            if mtime_dt[f["file"]] < lo:
                continue  # 廉价元数据预筛：锚点窗内没被写过的文件不整扫
            best: float | None = None
            for ev in events_for(f["file"]):
                raw_ts = ev.get("ts")
                if not raw_ts:
                    continue
                try:
                    ets = _parse_ts(raw_ts)
                except ValueError:
                    continue
                delta = (ets - anchor.ts).total_seconds()
                if abs(delta) <= MATCH_WINDOW_SEC \
                        and text_matches(anchor.text, ev.get("text", "")):
                    ad = abs(delta)
                    if best is None or ad < best:
                        best = ad
            if best is not None:
                text_edges.append((best, ai, anchor, f["file"]))

    anchors_done: set[int] = set()
    text_edges.sort(key=lambda e: (e[0], e[1]))
    for _d, ai, anchor, fname in text_edges:
        if ai in anchors_done:
            continue
        owner = claimed.get(fname)
        if owner is not None and owner != anchor.key:
            continue  # 更准的锚点已把文件判给别的属主，不抢
        claimed[fname] = anchor.key
        anchors_done.add(ai)
        win[ai] = fname

    # 3) 出生锚点：窗内全局唯一新文件（多文件必须文本边），且首条用户
    #    文本 squeeze 后对得上 —— 时间巧合但内容无关的本地首消息不许错认
    for ai, anchor in enumerate(anchor_list):
        if ai in frozen or ai in anchors_done:
            continue
        newborn = [f["file"] for f in files
                   if first_dt[f["file"]] is not None
                   and -START_TOLERANCE_SEC
                   <= (first_dt[f["file"]] - anchor.ts).total_seconds()
                   <= MATCH_WINDOW_SEC]
        if len(newborn) != 1 or newborn[0] in claimed:
            continue
        first_events = events_for(newborn[0])
        first_text = first_events[0].get("text", "") if first_events else ""
        if text_matches(anchor.text, first_text):
            claimed[newborn[0]] = anchor.key
            anchors_done.add(ai)
            win[ai] = newborn[0]

    # 4) 决定落盘冻结，活跃文件续写导致签名失效也不再反复整扫：
    #    正认领立即冻结；负结果（None）多等 NEGATIVE_FREEZE_GRACE_SEC，
    #    给窗内最后时刻事件的刷盘留时间（首个关窗轮询可能早于刷盘）
    closed = {}
    for ai in range(len(anchor_list)):
        if ai in frozen:
            continue
        anchor = anchor_list[ai]
        verdict = win.get(ai)
        edge = anchor.ts + timedelta(seconds=MATCH_WINDOW_SEC)
        if verdict is not None:
            closed[anchor] = verdict
        elif now >= edge + timedelta(seconds=NEGATIVE_FREEZE_GRACE_SEC):
            closed[anchor] = None
    _commit_frozen(closed, now)

    owned: dict = defaultdict(list)
    for fname, key in claimed.items():
        ft = first_dt[fname] or mtime_dt[fname]
        owned[key].append((ft, by_name[fname]))
    return set(claimed), owned


def _resolve_group(cwd: str | None, plist: list[dict], result: dict,
                   anchor_store=None) -> None:
    files = list_files_cached(cwd)
    if not files:
        return
    starts: dict = {}
    for p in plist:
        if p.get("started"):
            starts[p["key"]] = datetime.fromisoformat(p["started"]).astimezone(timezone.utc)

    # ---- phase-0：注入锚点硬认领，命中文件直接移出两轮匹配池 ----
    # 放在 starts 空判断之前：锚点自带属主 key，PEB 取不到启动时间的进程
    # （started=None）两轮匹配本就无证据，锚点是它唯一的归属手段。
    anchor_owned: dict = defaultdict(list)
    if anchor_store is not None:
        live_keys = {p["key"] for p in plist}
        # 回放锚点可能指向已退出 pid：死属主永不出结果，只会把文件移出活
        # 进程的匹配池（resume 场景被饿死），一律过滤
        anchor_list = [a for a in anchor_store.recent(cwd or "")
                       if a.key in live_keys]
        if anchor_list:
            claimed_names, anchor_owned = _phase0_claims(cwd, anchor_list, files)
            if claimed_names:
                files = [f for f in files if f["file"] not in claimed_names]

    if not starts:
        # 全组进程启动时间未知：两轮时间匹配无法进行，只兑现锚点认领
        for key, lst in anchor_owned.items():
            if key in result and lst:
                lst.sort(key=lambda x: x[0])
                f = lst[-1][1]
                result[key] = {"file": f["file"], "title": _file_title(f)}
        return
    # 确定性顺序：文件按首事件时间、进程按启动时间；平手时匹配结果可复现
    keys = sorted(starts, key=lambda k: (starts[k], str(k)))

    # ---- 第一轮：首事件时间在启动窗口内的文件，做排他主匹配 ----
    born = [(i, datetime.fromisoformat(f["first_ts"]))
            for i, f in enumerate(files) if f["first_ts"]]
    born.sort(key=lambda x: x[1])
    costs: list[list[float | None]] = []
    cands: list[tuple[int, float, int]] = []
    for fi, ft in born:
        row: list[float | None] = []
        for j, key in enumerate(keys):
            delta = (ft - starts[key]).total_seconds()
            if delta < -START_TOLERANCE_SEC or delta > MAX_LOOKBACK_GAP_SEC:
                row.append(None)
            else:
                c = max(delta, 0.0)
                row.append(c)
                cands.append((fi, c, j))
        costs.append(row)
    row_of = {fi: ri for ri, (fi, _ft) in enumerate(born)}
    primary_rows = _assign_exclusive(costs)
    extra_rows = _attach_remaining(
        [(row_of[fi], c, j) for fi, c, j in cands], primary_rows)

    # phase-0 锚点文件作为属主的初始拥有物；它们已移出匹配池，两轮不会再碰
    owned: dict = defaultdict(list)
    for key, lst in anchor_owned.items():
        owned[key].extend(lst)
    for ri, j in primary_rows.items():
        fi, ft = born[ri]
        owned[keys[j]].append((ft, files[fi]))
    for ri, j in extra_rows.items():
        fi, ft = born[ri]
        owned[keys[j]].append((ft, files[fi]))

    current: dict = {}
    for key, lst in owned.items():
        lst.sort(key=lambda x: x[0])
        current[key] = lst[-1][1]
    # 第一轮属主拥有的所有文件（含 /clear 前的旧 session）都不再参与第二轮
    claimed = {f["file"] for lst in owned.values() for _ts, f in lst}

    # ---- 第二轮：无文件进程按 mtime 对续写旧文件（resume）做排他匹配 ----
    homeless = [k for k in keys if k not in current]
    if homeless:
        resume = [(i, datetime.fromisoformat(f["mtime"]))
                  for i, f in enumerate(files) if f["file"] not in claimed]
        resume.sort(key=lambda x: x[1])
        rcosts: list[list[float | None]] = []
        rcands: list[tuple[int, float, int]] = []
        for fi, mt in resume:
            row: list[float | None] = []
            for j, key in enumerate(homeless):
                delta = (mt - starts[key]).total_seconds()
                # 与第一轮同一封顶：gap > 30min 不许靠 mtime 回溯偷新文件，
                # 宁可诚实返回 None（注入场景有 phase-0 锚点，不靠这里）
                if delta < -START_TOLERANCE_SEC or delta > MAX_LOOKBACK_GAP_SEC:
                    row.append(None)
                else:
                    c = max(delta, 0.0)
                    row.append(c)
                    rcands.append((fi, c, j))
            rcosts.append(row)
        r_row_of = {fi: ri for ri, (fi, _mt) in enumerate(resume)}
        r_primary = _assign_exclusive(rcosts)
        r_extra = _attach_remaining(
            [(r_row_of[fi], c, j) for fi, c, j in rcands], r_primary)
        wins: dict = defaultdict(list)  # key -> [(mtime, file_dict)]
        for ri, j in list(r_primary.items()) + list(r_extra.items()):
            fi, mt = resume[ri]
            wins[homeless[j]].append((mt, files[fi]))
        # 与第一轮语义对齐：同一进程的当前文件取 mtime 最新者
        for key, lst in wins.items():
            lst.sort(key=lambda x: x[0])
            current[key] = lst[-1][1]

    for key, f in current.items():
        # 双保险：锚点已按 live key 过滤，理论上 key 必在 result 内
        if key in result:
            result[key] = {"file": f["file"], "title": _file_title(f)}


def _tool_detail(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().splitlines()
        return cmd[0][:160] if cmd else "bash"
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return str(inp.get("file_path") or "")
    if name in ("Glob", "Grep"):
        return str(inp.get("pattern") or "")
    if name in ("WebFetch",):
        return str(inp.get("url") or "")
    if name in ("WebSearch",):
        return str(inp.get("query") or "")
    if name in ("Task", "Agent"):
        return str(inp.get("description") or name)
    return name


def parse_line(ln: str) -> dict | None:
    try:
        d = json.loads(ln)
    except ValueError:
        return None
    if d.get("isSidechain"):
        return None
    t = d.get("type")
    ts = d.get("timestamp")
    msg = d.get("message")
    if t == "user" and isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return {"role": "user", "kind": "text", "text": content, "ts": ts}
        if isinstance(content, list):
            texts = []
            results = []
            errored = False
            for b in content:
                if not isinstance(b, dict):
                    continue
                btxt = b.get("text")  # 外部 jsonl 可能写出 text:null/非字符串
                if b.get("type") == "text" and isinstance(btxt, str) and btxt.strip():
                    texts.append(btxt.strip())
                elif b.get("type") == "tool_result":
                    raw = b.get("content")
                    txt = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                    tag = "⚠ 工具报错" if b.get("is_error") else "↩ 工具结果"
                    results.append(f"{tag}: {txt[:PREVIEW_LIMIT]}")
                    errored = errored or bool(b.get("is_error"))
            # 用户真有发言时只回提示词：工具结果是过程噪音，前端另作折叠组，
            # 绝不拼进用户原话（否则用户提示词高亮会把工具结果一起点亮）。
            if texts:
                return {"role": "user", "kind": "text",
                        "text": "\n".join(texts), "ts": ts}
            if results:
                return {"role": "user", "kind": "toolresult", "is_error": errored,
                        "text": "\n".join(results), "ts": ts}
            return None
    if t == "assistant" and isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            texts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                btxt = b.get("text")
                if bt == "text" and isinstance(btxt, str) and btxt.strip():
                    texts.append(btxt.strip())
                elif bt == "tool_use":
                    return {"role": "assistant", "kind": "tool",
                            "name": b.get("name", "?"),
                            "detail": _tool_detail(b.get("name", ""), b.get("input") or {}),
                            "ts": ts}
            if texts:
                return {"role": "assistant", "kind": "text",
                        "text": "\n".join(texts), "ts": ts}
    return None


# 「只看我」视图剔除的非人工键入消息前缀（harness/agent 注入）。
# 一律 strip 后前缀匹配：用户打字内容里提到这些词（如"出现<task-notification>…"）不中招。
_SYNTHETIC_PREFIXES = (
    "<task-notification",              # 后台任务完成通知
    "<system-reminder",                # 系统提醒注入
    "<command-",                       # 斜杠命令回显（command-name/message/args）
    "<local-command",                  # 本地命令输出回显
    "<cross-session-message",          # 跨会话消息投递
    "<bash-",                          # bash-input/bash-output 包装
    "Caveat:",                         # 续写摘要的免责前缀
    "This session is being continued",  # 上下文耗尽自动续会话的总结注入
)
_IMAGE_LINE_RE = re.compile(r"\[Image[^\n]*\]")


def is_synthetic_prompt(text: str) -> bool:
    """这条"用户消息"是否非本人键入（harness/agent 注入的占位/通知/摘要）。

    「只看我」视图据此整段剔除。图片例外：以 [Image…] 占位开头但后面还有
    真人文字的（贴图+说明）保留；纯图片占位剔除。
    """
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("[Image"):
        return not _IMAGE_LINE_RE.sub("", t).strip()
    return t.startswith(_SYNTHETIC_PREFIXES)


def drop_synthetic(events: list[dict]) -> list[dict]:
    """只看我视图出口：剔除非人工键入的用户消息（见 is_synthetic_prompt）。"""
    return [e for e in events if not is_synthetic_prompt(e.get("text") or "")]


def _read_complete(path: Path, start: int, end: int) -> tuple[list[str], int]:
    """读 [start:end)，只返回到最后一个换行符为止的完整行及新的偏移。"""
    if end <= start:
        return [], start
    with path.open("rb") as f:
        f.seek(start)
        chunk = f.read(end - start)
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return [], start
    consumed = last_nl + 1
    text = chunk[:consumed].decode("utf-8", "replace")
    return [ln for ln in text.splitlines() if ln.strip()], start + consumed


def read_events(cwd: str | None, file: str | None, after: int = 0) -> dict:
    d = project_dir(cwd)
    if not d or not file:
        return {"events": [], "size": after, "found": False}
    return read_events_dir(d.name, file, after)


def read_user_events(cwd: str | None, file: str | None) -> dict:
    d = project_dir(cwd)
    if not d or not file:
        return {"events": [], "size": 0, "found": False}
    return read_user_events_dir(d.name, file)


# 「只看我」整扫缓存：{路径: ((mtime_ns, size), events)}，文件未变不重复解析
_user_events_cache: dict[str, tuple] = {}
_USER_CACHE_MAX = 64


def read_user_events_dir(slug: str | None, file: str | None) -> dict:
    """全文件扫描，只回用户提示词（role=user & kind=text）。「只看我」视图专用。

    普通镜像只读尾部 TAIL_BYTES 窗口，长会话的早期提示词注定被裁掉；
    这里整扫补偿。提示词事件体积小（百级），按文件签名缓存后 2.5s 轮询
    不会反复解析大文件。校验规则与 read_events_dir 完全一致。
    """
    if not slug or not file or not SAFE_DIR_RE.match(slug):
        return {"events": [], "size": 0, "found": False}
    if not SAFE_NAME_RE.match(file):
        return {"events": [], "size": 0, "found": False}
    d = PROJECTS_DIR / slug
    path = d / file
    if not path.is_file() or path.parent != d or d.parent != PROJECTS_DIR:
        return {"events": [], "size": 0, "found": False}
    st = path.stat()
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    cached = _user_events_cache.get(key)
    if cached and cached[0] == sig:
        events = cached[1]
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        events = [e for ln in text.splitlines()
                  if (e := parse_line(ln)) is not None
                  and e["role"] == "user" and e["kind"] == "text"]
        if len(_user_events_cache) >= _USER_CACHE_MAX:
            _user_events_cache.pop(next(iter(_user_events_cache)))
        _user_events_cache[key] = (sig, events)
    return {"events": events, "size": st.st_size, "found": True}


def read_events_dir(slug: str | None, file: str | None, after: int = 0) -> dict:
    """历史视图直读：dir 是 projects 下的目录名（encode_cwd 产物），不经 cwd 反推。"""
    if not slug or not file or not SAFE_DIR_RE.match(slug):
        return {"events": [], "size": after, "found": False}
    if not SAFE_NAME_RE.match(file):
        # file 来自网络参数：只允许 uuid 式文件名，挡住 ..\\.. 等目录穿越
        return {"events": [], "size": after, "found": False}
    d = PROJECTS_DIR / slug
    path = d / file
    if not path.is_file() or path.parent != d or d.parent != PROJECTS_DIR:
        return {"events": [], "size": 0, "found": False}
    size = path.stat().st_size
    # 文件被截断/切换（resume 或 /clear 后新 session），从头追赶
    if size < after:
        after = 0
    if after == 0:
        start = max(0, size - TAIL_BYTES)
        # 尾部窗口内连一个换行都没有 = 最后一行是巨型 JSON（实测 15MB 文件
        # 最后一行 >128KB，工具结果把一条记录撑得很大）。扩大到 4MB 窗口重读。
        lines, new_after = _read_complete(path, start, size)
        if not lines and start > 0:
            lines, new_after = _read_complete(path, max(0, size - MAX_TAIL_BYTES), size)
    else:
        start = after
        lines, new_after = _read_complete(path, start, size)
    events = [e for ln in lines if (e := parse_line(ln))]
    return {"events": events, "size": new_after, "found": True}


# ---------------- 历史会话（跨项目扫盘视图） ----------------

def bridge_session_ids() -> set[str]:
    """微信桥 sessions.log 里的全部 session id（含滚动备份；文件缺失时为空集）。

    桥 DONE 行格式：... session=<uuid> jsonl=<路径>；START 行的 resume=<前8位>
    太短不可靠，只认 DONE 的完整 id。
    """
    ids: set[str] = set()
    try:
        logs = sorted(BRIDGE_SESSIONS_LOG.parent.glob(
            BRIDGE_SESSIONS_LOG.name + "*"))
    except OSError:
        return ids
    for p in logs:
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ids.update(m.group(1) for m in _BRIDGE_SID_RE.finditer(text))
    return ids


def list_history(limit: int = HISTORY_LIMIT,
                 ttl: float = HISTORY_CACHE_TTL) -> list[dict]:
    """跨项目历史会话：全部 projects 目录按 mtime 取最近 limit 个 jsonl。

    活会话列表只枚举进程，claude -p（微信桥任务）跑完即从会话台消失 ——
    本视图直接扫盘补上这个盲区。指纹 = 最近 limit 个文件的
    (目录/文件名, mtime_ns, size)，未变则 TTL 内不重读文件头。
    """
    try:
        files = [(f, f.stat())
                 for d in PROJECTS_DIR.iterdir() if d.is_dir()
                 for f in d.glob("*.jsonl")]
    except OSError:
        return []
    files.sort(key=lambda x: x[1].st_mtime_ns, reverse=True)
    top = files[:limit]
    fp = tuple((f.parent.name, f.name, st.st_mtime_ns, st.st_size)
               for f, st in top)
    now = time.monotonic()
    if ttl > 0 and _history_cache["fp"] == fp \
            and now - _history_cache["ts"] < ttl:
        return _history_cache["items"]
    sids = bridge_session_ids()
    items = []
    for f, st in top:
        title, preview, first_ts = _scan_head(f)
        items.append({
            "dir": f.parent.name,
            "file": f.name,
            "mtime": datetime.fromtimestamp(st.st_mtime,
                                           tz=timezone.utc).isoformat(),
            "size": st.st_size,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "title": title,
            "preview": preview,
            "bridge": f.stem in sids,
        })
    _history_cache.update(ts=now, fp=fp, items=items)
    return items
