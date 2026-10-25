# -*- coding: utf-8 -*-
"""promptlog：配置加载 / 建表 / 写入 / 降级（fake pymysql，不碰真 MySQL）。"""
import json
from datetime import datetime, timezone

import pytest

from server import promptlog


# ---------- fake pymysql ----------

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchall(self):
        return list(self.conn.fake.rows)


class _FakeConn:
    def __init__(self, fake, kwargs):
        self.fake = fake
        self.kwargs = kwargs
        self.executed = fake.executed

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self)


class FakePymysql:
    """记录所有 connect 参数与 execute 语句；fail_times>0 时前 N 次 connect 抛错。"""

    def __init__(self, fail_times=0):
        self.executed = []
        self.connects = []
        self.fail_times = fail_times
        self.rows = []  # fetchall() 对所有查询返回的行（测试按场景设置）

    def connect(self, **kwargs):
        self.connects.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("fake connect error")
        return _FakeConn(self, kwargs)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    promptlog.reset_store()
    monkeypatch.delenv("CRW_MYSQL_HOST", raising=False)
    yield
    promptlog.reset_store()


CFG = {"host": "127.0.0.1", "port": 3306, "user": "root",
       "password": "pw", "database": "testdb"}


# ---------- 配置加载 ----------

def test_config_from_file(tmp_path):
    p = tmp_path / "mysql.json"
    p.write_text(json.dumps(CFG), encoding="utf-8")
    cfg = promptlog.load_config(p)
    assert cfg["database"] == "testdb" and cfg["port"] == 3306


def test_config_env_overrides_file(monkeypatch, tmp_path):
    p = tmp_path / "mysql.json"
    p.write_text(json.dumps(CFG), encoding="utf-8")
    monkeypatch.setenv("CRW_MYSQL_DATABASE", "envdb")
    monkeypatch.setenv("CRW_MYSQL_PORT", "3307")
    cfg = promptlog.load_config(p)
    assert cfg["database"] == "envdb" and cfg["port"] == 3307


def test_config_missing_and_broken(tmp_path):
    assert promptlog.load_config(tmp_path / "nope.json") is None
    p = tmp_path / "mysql.json"
    p.write_text("{bad json", encoding="utf-8")
    assert promptlog.load_config(p) is None
    p.write_text(json.dumps({"host": "x"}), encoding="utf-8")  # 缺 database
    assert promptlog.load_config(p) is None


# ---------- 建表与写入 ----------

def test_schema_and_insert():
    fake = FakePymysql()
    store = promptlog.MySQLPromptLog(CFG, pymysql_mod=fake)
    sqls = [s for s, _ in fake.executed]
    assert any("CREATE DATABASE IF NOT EXISTS `testdb`" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS prompt_log" in s for s in sqls)
    store.record({"source": "single", "batch_id": "", "target": "pid 1",
                  "target_title": "t", "cwd": "c", "prompt": "你好",
                  "ok": 1, "error": None})
    insert = fake.executed[-1]
    assert "INSERT INTO prompt_log" in insert[0]
    assert insert[1]["prompt"] == "你好" and insert[1]["ok"] == 1


def test_reconnect_retry_once():
    import threading
    fake = FakePymysql()
    store = promptlog.MySQLPromptLog.__new__(promptlog.MySQLPromptLog)
    store._cfg, store._mysql = CFG, fake
    store._lock = threading.RLock()  # 与生产一致：持锁递归重试必须 RLock
    fake.fail_times = 1  # 写入时第一次连接失败 → 重试成功
    store.record({"source": "single", "batch_id": "", "target": "pid 1",
                  "target_title": "", "cwd": "", "prompt": "x", "ok": 1,
                  "error": None})
    assert any("INSERT" in s for s, _ in fake.executed)


# ---------- log() 降级：任何失败不抛出 ----------

def test_log_without_config_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(promptlog, "_CONFIG_PATH", tmp_path / "none.json")
    promptlog.log("single", "pid 1", "提示词")  # 不抛异常即通过


def test_log_mysql_down_is_noop(tmp_path, monkeypatch):
    p = tmp_path / "mysql.json"
    p.write_text(json.dumps(CFG), encoding="utf-8")
    monkeypatch.setattr(promptlog, "_CONFIG_PATH", p)
    monkeypatch.setattr(promptlog, "MySQLPromptLog",
                        lambda cfg: (_ for _ in ()).throw(OSError("down")))
    promptlog.log("single", "pid 1", "提示词")  # 不抛异常即通过


# ---------- recent_rows：冷启动锚点回放的只读查询 ----------

def test_recent_rows_sql_filter_and_target_parsing():
    fake = FakePymysql()
    naive_ts = datetime(2026, 10, 21, 12, 41, 2)  # MySQL 存本机墙上时间（naive）
    fake.rows = [
        ("pid 24580", r"E:\1workspace", "目前效果怎么样", naive_ts),
        ("mgr 2", r"C:\Users\4v", "托管会话注入", naive_ts),
        ("wechat 9", r"E:\x", "无法解析的目标", naive_ts),
    ]
    store = promptlog.MySQLPromptLog(CFG, pymysql_mod=fake)
    rows = store.recent_rows(hours=2)
    select = [s for s, _ in fake.executed if "FROM prompt_log" in s][0]
    params = [p for s, p in fake.executed if "FROM prompt_log" in s][0]
    assert "ok = 1" in select or "ok=1" in select
    assert "INTERVAL %s HOUR" in select
    assert params == (2,)
    assert len(rows) == 2  # "wechat 9" 不可解析，丢弃
    assert rows[0]["kind"] == "ext" and rows[0]["id"] == 24580
    assert rows[0]["cwd"] == r"E:\1workspace" and rows[0]["prompt"] == "目前效果怎么样"
    assert rows[1]["kind"] == "mgr" and rows[1]["id"] == 2
    # naive 本机时间被解释成本地时区并转成 aware UTC
    assert rows[0]["ts"].tzinfo is not None
    local_tz = datetime.now().astimezone().tzinfo
    expected = naive_ts.replace(tzinfo=local_tz).astimezone(timezone.utc)
    assert rows[0]["ts"] == expected and rows[1]["ts"] == expected


def test_recent_rows_module_fn_degrades_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(promptlog, "_CONFIG_PATH", tmp_path / "none.json")
    assert promptlog.recent_rows() == []  # 不抛异常即通过


def test_recent_rows_query_failure_returns_empty():
    class _BoomCursor(_FakeCursor):
        def fetchall(self):
            raise OSError("connection lost")

    class _BoomConn(_FakeConn):
        def cursor(self):
            return _BoomCursor(self)

    class BoomPymysql(FakePymysql):
        def connect(self, **kwargs):
            self.connects.append(kwargs)
            return _BoomConn(self, kwargs)

    store = promptlog.MySQLPromptLog(CFG, pymysql_mod=BoomPymysql())
    assert store.recent_rows() == []  # 重试一次后吞掉，冷启动绝不因此挂掉


def test_log_truncates_long_fields(tmp_path, monkeypatch):
    fake = FakePymysql()
    real_cls = promptlog.MySQLPromptLog  # 先存真类：patch 后模块属性已是替身
    monkeypatch.setattr(promptlog, "MySQLPromptLog",
                        lambda cfg: real_cls(cfg, fake))
    p = tmp_path / "mysql.json"
    p.write_text(json.dumps(CFG), encoding="utf-8")
    monkeypatch.setattr(promptlog, "_CONFIG_PATH", p)
    promptlog.log("broadcast", "pid 1", "正文", ok=False, error="e" * 600,
                  batch_id="b1", target_title="t" * 300, cwd="c" * 600)
    row = fake.executed[-1][1]
    assert len(row["error"]) == 512
    assert len(row["target_title"]) == 255
    assert len(row["cwd"]) == 512
    assert row["ok"] == 0 and row["batch_id"] == "b1"
