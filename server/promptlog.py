# -*- coding: utf-8 -*-
"""提示词落 MySQL：会话台发出的每一条提示词（单发/广播）写一行 prompt_log。

- 配置来源：环境变量 CRW_MYSQL_* > data/mysql.json > 不可用即降级（只告警）
- 库/表自动创建；连接断开自动重连一次
- **任何失败都不允许影响发送主流程**（log() 内部全捕获）
- 用户直接查库，本模块不提供读接口

表结构（claude_remote_web.prompt_log）：
  source:    single（单个会话发送）| broadcast（广播）
  batch_id:  同一次广播的所有行共享一个 uuid，方便按批查
  target:    "pid 1234" / "mgr 2"
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("crw.promptlog")

TABLE = "prompt_log"

_TARGET_RE = re.compile(r"^(pid|mgr)\s+(\d+)$")

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  source VARCHAR(16) NOT NULL,
  batch_id VARCHAR(36) NOT NULL DEFAULT '',
  target VARCHAR(128) NOT NULL DEFAULT '',
  target_title VARCHAR(255) NOT NULL DEFAULT '',
  cwd VARCHAR(512) NOT NULL DEFAULT '',
  prompt TEXT NOT NULL,
  ok TINYINT NOT NULL DEFAULT 1,
  error VARCHAR(512) DEFAULT NULL,
  INDEX idx_ts (ts),
  INDEX idx_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_INSERT_SQL = (
    f"INSERT INTO {TABLE} (source, batch_id, target, target_title, cwd,"
    " prompt, ok, error)"
    " VALUES (%(source)s, %(batch_id)s, %(target)s, %(target_title)s,"
    " %(cwd)s, %(prompt)s, %(ok)s, %(error)s)"
)

# ts 列是 MySQL 会话时区的 naive DATETIME（DEFAULT CURRENT_TIMESTAMP(3)），
# 故窗口裁剪也用 NOW(3)（同会话时区），不能用 UTC_TIMESTAMP。
_SELECT_RECENT_SQL = (
    f"SELECT target, cwd, prompt, ts FROM {TABLE} "
    "WHERE ok = 1 AND ts >= DATE_SUB(NOW(3), INTERVAL %s HOUR) "
    "ORDER BY ts ASC"
)

# 发送回执按目标查询：含失败行（ok=0），前端要展示「✗ 注入失败」
_SELECT_TARGET_SQL = (
    f"SELECT ts, prompt, ok, error, cwd FROM {TABLE} "
    "WHERE target = %s AND ts >= DATE_SUB(NOW(3), INTERVAL %s HOUR) "
    "ORDER BY ts ASC"
)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "mysql.json"


def _to_utc(ts) -> datetime | None:
    """MySQL naive 墙上时间 → aware UTC。

    本部署 MySQL 与本机同时区（本地 Windows MySQL），naive DATETIME 按本机
    时区解释。若改用 UTC 时区的 MySQL（Docker 等），窗口会整体偏移，
    届时改由 SQL 侧统一时区。
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        return ts.replace(tzinfo=local_tz).astimezone(timezone.utc)
    return ts.astimezone(timezone.utc)


def new_batch_id() -> str:
    return uuid.uuid4().hex


def load_config(config_path: Path | None = None) -> dict | None:
    """环境变量优先；其次 data/mysql.json；都没有返回 None（功能关闭）。"""
    env = {k.lower(): os.environ.get(f"CRW_MYSQL_{k.upper()}")
           for k in ("host", "port", "user", "password", "database")}
    if any(v for v in env.values()):
        cfg = {"host": "127.0.0.1", "port": 3306, "user": "root",
               "password": "", "database": "claude_remote_web"}
        cfg.update({k: v for k, v in env.items() if v})
        cfg["port"] = int(cfg["port"])
        return cfg
    path = Path(config_path) if config_path else _CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.warning("📋 mysql.json 解析失败（%s），提示词落库关闭", e)
        return None
    if not isinstance(data, dict) or not data.get("database"):
        return None
    cfg = {"host": data.get("host", "127.0.0.1"),
           "port": int(data.get("port", 3306)),
           "user": data.get("user", "root"),
           "password": data.get("password", ""),
           "database": data["database"]}
    return cfg


class MySQLPromptLog:
    """MySQL 存储：自动建库建表，断连重试一次。测试注入 fake pymysql。"""

    def __init__(self, cfg: dict, pymysql_mod=None):
        self._cfg = cfg
        if pymysql_mod is None:
            import pymysql as pymysql_mod
        self._mysql = pymysql_mod
        # 必须用 RLock：_execute 断连重试是在持锁状态下递归调用自身
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self, with_db: bool = True):
        kwargs = dict(
            host=self._cfg.get("host", "127.0.0.1"),
            port=int(self._cfg.get("port", 3306)),
            user=self._cfg.get("user", "root"),
            password=self._cfg.get("password", ""),
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
        )
        if with_db:
            kwargs["database"] = self._cfg["database"]
        return self._mysql.connect(**kwargs)

    def _ensure_schema(self) -> None:
        db = self._cfg["database"]
        with self._connect(with_db=False) as conn, conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db}` DEFAULT CHARSET utf8mb4")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
        logger.info("📋 提示词表就绪：MySQL %s.%s", self._cfg.get("host"), db)

    def _execute(self, sql: str, params=None, retry: bool = True):
        with self._lock:
            try:
                with self._connect() as conn, conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            except Exception:  # noqa: BLE001 - 断连重试一次
                if not retry:
                    raise
                logger.warning("📋 MySQL 写入失败，重连重试一次", exc_info=True)
                return self._execute(sql, params, retry=False)

    def record(self, row: dict) -> None:
        self._execute(_INSERT_SQL, row)

    def recent_rows(self, hours: float = 2.0) -> list[dict]:
        """近 hours 小时内成功注入的行（冷启动锚点回放用）。

        返回 [{"kind": "ext"|"mgr", "id": int, "cwd": str,
        "prompt": str, "ts": aware UTC datetime}]，无法解析 target 的行丢弃。
        只读路径，任何异常都吞成空列表 —— 绝不能拖垮启动。
        """
        try:
            raw = self._execute(_SELECT_RECENT_SQL, (hours,)) or []
        except Exception:  # noqa: BLE001
            logger.warning("📋 MySQL 读取近期提示词失败（不影响启动）",
                           exc_info=True)
            return []
        rows = []
        for target, cwd, prompt, ts in raw:
            m = _TARGET_RE.match((target or "").strip())
            if not m or not ts:
                continue
            rows.append({
                "kind": "ext" if m.group(1) == "pid" else "mgr",
                "id": int(m.group(2)),
                "cwd": cwd or "",
                "prompt": prompt or "",
                "ts": _to_utc(ts),
            })
        return rows

    def recent_for_target(self, target: str, hours: float = 36.0) -> list[dict]:
        """某目标近 hours 小时的全部注入行（含 ok=0 失败行），发送回执用。

        返回 [{"ts": aware UTC, "prompt", "ok": bool, "error", "cwd"}]，
        按注入时间升序。只读路径，查询失败抛给模块级入口降级。
        """
        raw = self._execute(_SELECT_TARGET_SQL, (target, hours)) or []
        return [{"ts": _to_utc(ts), "prompt": prompt or "",
                 "ok": bool(ok), "error": error or None, "cwd": cwd or ""}
                for ts, prompt, ok, error, cwd in raw if ts]


# ---------------- 单例与降级 ----------------

_store = None            # MySQLPromptLog | None（None=已判定不可用）
_tried = False
_store_lock = threading.Lock()


def _get_store(config_path: Path | None = None):
    """惰性单例；不可用时返回 None（每次调用重试一次连接，MySQL 恢复后自愈）。"""
    global _store, _tried
    with _store_lock:
        if _store is not None:
            return _store
        cfg = load_config(config_path)
        if cfg is None:
            if not _tried:
                logger.info("📋 未配置 MySQL（CRW_MYSQL_* / data/mysql.json），"
                            "提示词不落库")
            _tried = True
            return None
        try:
            _store = MySQLPromptLog(cfg)
            return _store
        except Exception as e:  # noqa: BLE001
            if not _tried:
                logger.warning("📋 MySQL 不可用（%s），提示词不落库（不影响发送）", e)
            _tried = True
            return None


def reset_store() -> None:
    """重置单例（测试用）。"""
    global _store, _tried
    with _store_lock:
        _store = None
        _tried = False


def recent_rows(hours: float = 2.0) -> list[dict]:
    """模块级只读入口：未配置/不可用时返回 []（冷启动回放必须无感降级）。"""
    try:
        store = _get_store()
        return store.recent_rows(hours) if store is not None else []
    except Exception:  # noqa: BLE001
        logger.warning("📋 读取近期提示词失败（不影响启动）", exc_info=True)
        return []


def recent_for_target(target: str, hours: float = 36.0) -> list[dict] | None:
    """模块级只读入口：未配置/不可用时返回 None（调用方据此降级到内存锚点）。"""
    try:
        store = _get_store()
        if store is None:
            return None
        return store.recent_for_target(target, hours)
    except Exception:  # noqa: BLE001
        logger.warning("📋 读取目标提示词失败（回执降级到内存锚点）", exc_info=True)
        return None


def log(source: str, target: str, prompt: str, ok: bool = True,
        error: str | None = None, batch_id: str = "",
        target_title: str = "", cwd: str = "") -> None:
    """落一行提示词；任何失败都不允许影响发送主流程。"""
    try:
        store = _get_store()
        if store is None:
            return
        store.record({
            "source": source, "batch_id": batch_id, "target": target,
            "target_title": target_title[:255], "cwd": cwd[:512],
            "prompt": prompt, "ok": 1 if ok else 0,
            "error": (error or None) and str(error)[:512],
        })
    except Exception:  # noqa: BLE001
        logger.warning("📋 提示词落库失败（不影响发送）", exc_info=True)
