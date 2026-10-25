"""SQLite（WAL）唯一真源与 schema。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    device_id TEXT UNIQUE,
    display_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS categories (
    key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    strategy TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    slots_json TEXT NOT NULL DEFAULT '{}',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    budget REAL,
    status TEXT NOT NULL DEFAULT 'collecting',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_booking_user ON bookings(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_booking_category ON bookings(category, status);

CREATE TABLE IF NOT EXISTS watch_tasks (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    category TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    window_end TEXT NOT NULL DEFAULT '',
    next_run_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    last_decision TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watch_next ON watch_tasks(status, next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_active ON watch_tasks(booking_id) WHERE status='running';

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL,
    tax REAL NOT NULL DEFAULT 0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'mock',
    confidence REAL NOT NULL DEFAULT 0.8,
    captured_at TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    hour_bucket TEXT NOT NULL,
    UNIQUE(category, platform, item_id, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_price_lookup ON price_snapshots(category, captured_at);
CREATE INDEX IF NOT EXISTS idx_price_item ON price_snapshots(category, item_id, captured_at);

CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    coupon_code TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    coupon_type TEXT NOT NULL DEFAULT 'amount',
    amount REAL NOT NULL DEFAULT 0,
    discount_rate REAL NOT NULL DEFAULT 0,
    min_spend REAL NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'mock',
    confidence REAL NOT NULL DEFAULT 0.8,
    status TEXT NOT NULL DEFAULT 'active',
    collected_at TEXT NOT NULL,
    quarantined INTEGER NOT NULL DEFAULT 0,
    UNIQUE(category, platform, coupon_code)
);
CREATE INDEX IF NOT EXISTS idx_coupon_active ON coupons(category, status, valid_to);

CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL,
    coupon_id TEXT,
    coupon_code TEXT,
    discount REAL NOT NULL DEFAULT 0,
    paid_price REAL NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    quoted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offer_booking ON offers(booking_id, quoted_at);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    list_price REAL NOT NULL,
    coupon_id TEXT,
    discount REAL NOT NULL DEFAULT 0,
    paid_price REAL NOT NULL,
    order_no TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    deep_link TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    paid_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    item_id TEXT NOT NULL DEFAULT '',
    item_name TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_order_booking ON orders(booking_id, created_at);

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'qr_session',
    ciphertext TEXT NOT NULL DEFAULT '',
    nonce TEXT NOT NULL DEFAULT '',
    key_version INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, platform)
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    ua TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    booking_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'web_push',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT '',
    window_key TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    UNIQUE(booking_id, type, window_key)
);
CREATE INDEX IF NOT EXISTS idx_notification_status ON notifications(status, scheduled_at);

CREATE TABLE IF NOT EXISTS check_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    fatal_count INTEGER NOT NULL DEFAULT 0,
    warn_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_check_run ON check_reports(run_at, category);

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_booking ON agent_events(booking_id, created_at);

CREATE TABLE IF NOT EXISTS decision_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id TEXT NOT NULL,
    category TEXT NOT NULL,
    input_features_json TEXT NOT NULL DEFAULT '{}',
    local_pred REAL NOT NULL DEFAULT 0,
    aggregated_pred REAL NOT NULL DEFAULT 0,
    decision_score REAL NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '',
    judge_used INTEGER NOT NULL DEFAULT 0,
    low_diversity INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_booking ON decision_runs(booking_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_category ON decision_runs(category, created_at);

CREATE TABLE IF NOT EXISTS model_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_run_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'predictor',
    prediction REAL,
    direction TEXT NOT NULL DEFAULT 'flat',
    confidence REAL NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    is_cold_start INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_modelpred_run ON model_predictions(decision_run_id);
CREATE INDEX IF NOT EXISTS idx_modelpred_model ON model_predictions(category, model, created_at);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    user_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    samples INTEGER NOT NULL DEFAULT 0,
    mae REAL NOT NULL DEFAULT 0,
    direction_accuracy REAL NOT NULL DEFAULT 0,
    brier REAL NOT NULL DEFAULT 0,
    reliability_score REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL,
    UNIQUE(category, provider, model)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _clean_param(value: Any) -> Any:
    """把终端解码产生的代理字符替换掉，避免 SQLite 编码时崩溃。"""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", errors="replace").decode("utf-8")
    return value


class Database:
    """单连接 + 进程内写锁，满足单用户单进程场景；WAL 支持并发读。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, [_clean_param(item) for item in params])
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [[_clean_param(item) for item in row] for row in rows])
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def backup(self, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()
        return dest

    def close(self) -> None:
        with self._lock:
            self._conn.close()
