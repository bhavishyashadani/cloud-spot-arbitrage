"""SQLite persistence for the control plane: price history, decisions,
backends (running VMs), migrations and the active route ("DNS record")."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ts REAL, key TEXT, cloud TEXT, region TEXT, zone TEXT, sku TEXT,
    price REAL, raw_price REAL
);
CREATE INDEX IF NOT EXISTS prices_ts ON prices(ts);
CREATE TABLE IF NOT EXISTS backends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT, cloud TEXT, region TEXT, zone TEXT, sku TEXT,
    instance_id TEXT, endpoint TEXT, status TEXT, market TEXT,
    created_ts REAL, deleted_ts REAL
);
CREATE TABLE IF NOT EXISTS migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL, finished_ts REAL, from_key TEXT, to_key TEXT,
    status TEXT, reason TEXT, steps TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    ts REAL, current_key TEXT, target_key TEXT, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(settings.db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def rows(sql: str, *args) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql: str, *args) -> int:
    with conn() as c:
        return c.execute(sql, args).lastrowid


def get(k: str, default=None):
    r = rows("SELECT v FROM kv WHERE k = ?", k)
    return json.loads(r[0]["v"]) if r else default


def put(k: str, v) -> None:
    execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", k, json.dumps(v))


def now() -> float:
    return time.time()
