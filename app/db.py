"""SQLite 数据层：建表、连接、常用查询。全部走一把可重入锁，兼容线程池与事件循环并发访问。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Sequence

from .config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,
    last_fetch_ts   INTEGER NOT NULL DEFAULT 0,
    last_status     TEXT NOT NULL DEFAULT '',
    last_node_count INTEGER NOT NULL DEFAULT 0,
    fetch_total     INTEGER NOT NULL DEFAULT 0,
    fetch_ok        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS nodes (
    fp            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    server        TEXT NOT NULL,
    port          INTEGER NOT NULL,
    raw           TEXT NOT NULL,
    source_name   TEXT NOT NULL DEFAULT '',
    first_seen    INTEGER NOT NULL,
    last_ok_ts    INTEGER NOT NULL DEFAULT 0,
    last_check_ts INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    fail_streak   INTEGER NOT NULL DEFAULT 0,
    alive         INTEGER NOT NULL DEFAULT 0,
    verified      INTEGER NOT NULL DEFAULT 0,
    udp_only      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nodes_alive ON nodes(alive, latency_ms);
CREATE INDEX IF NOT EXISTS idx_nodes_seen ON nodes(first_seen);

CREATE TABLE IF NOT EXISTS tokens (
    token         TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL,
    expires_ts    INTEGER NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    fetch_count   INTEGER NOT NULL DEFAULT 0,
    last_fetch_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    phase   TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON fetch_log(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：老库补字段，避免用户换版本后数据全废。"""
    with _lock:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
        if "verified" not in cols:
            conn.execute(
                "ALTER TABLE nodes ADD COLUMN verified INTEGER NOT NULL DEFAULT 0"
            )
            # 老库里有延迟数据的说明以前测通过，标记为已验证
            conn.execute(
                "UPDATE nodes SET verified=1 WHERE latency_ms IS NOT NULL AND alive=1"
            )
            conn.execute("UPDATE nodes SET alive=0 WHERE latency_ms IS NULL AND udp_only=0")
            conn.execute("UPDATE nodes SET alive=1 WHERE udp_only=1 AND verified=0")
            conn.commit()


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
            _migrate(_conn)
        return _conn


def init_db() -> None:
    get_conn()


def query(sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, args).fetchall()


def query_one(sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur


def execute_many(sql: str, seq: Sequence[Sequence[Any]]) -> None:
    if not seq:
        return
    with _lock:
        conn = get_conn()
        conn.executemany(sql, seq)
        conn.commit()


# ---------------- 日志 ----------------

def log(level: str, phase: str, message: str, source: str = "") -> None:
    execute(
        "INSERT INTO fetch_log (ts, level, phase, source, message) VALUES (?,?,?,?,?)",
        (int(time.time()), level, phase, source, message[:2000]),
    )


def recent_logs(limit: int = 200) -> list[dict]:
    rows = query("SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def trim_logs(keep: int = 2000) -> None:
    execute(
        "DELETE FROM fetch_log WHERE id <= (SELECT MAX(id) FROM fetch_log) - ?",
        (keep,),
    )


# ---------------- 设置 ----------------

def get_settings() -> dict:
    cfg = dict(DEFAULT_SETTINGS)
    rows = query("SELECT key, value FROM settings")
    for r in rows:
        try:
            cfg[r["key"]] = json.loads(r["value"])
        except Exception:
            continue
    return cfg


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
