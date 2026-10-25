"""仓储层：所有品类相关查询强制携带 category。"""

from __future__ import annotations

from typing import Any, Iterable

from ..models import (
    Booking,
    Coupon,
    Decision,
    ModelPrediction,
    Notification,
    Offer,
    Order,
    PriceSnapshot,
    now_iso,
    new_id,
    to_dict,
)
from .db import Database, _json


def _rows_to_coupons(rows: Iterable[dict]) -> list[Coupon]:
    return [Coupon.from_row(r) for r in rows]


def _rows_to_prices(rows: Iterable[dict]) -> list[PriceSnapshot]:
    return [PriceSnapshot.from_row(r) for r in rows]


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    def get_or_create(self, device_id: str, display_name: str = "用户") -> dict[str, Any]:
        existing = self.db.query_one("SELECT * FROM users WHERE device_id=?", [device_id])
        if existing:
            return existing
        user_id = new_id("u")
        self.db.execute(
            "INSERT INTO users (id, device_id, display_name, created_at) VALUES (?,?,?,?)",
            [user_id, device_id, display_name, now_iso()],
        )
        return self.db.query_one("SELECT * FROM users WHERE id=?", [user_id]) or {}

    def get(self, user_id: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM users WHERE id=?", [user_id])


class SessionRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: str) -> str:
        session_id = new_id("s")
        ts = now_iso()
        self.db.execute(
            "INSERT INTO sessions (id, user_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            [session_id, user_id, "active", ts, ts],
        )
        return session_id

    def get(self, session_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id:
            return self.db.query_one(
                "SELECT * FROM sessions WHERE id=? AND user_id=?", [session_id, user_id]
            )
        return self.db.query_one("SELECT * FROM sessions WHERE id=?", [session_id])


class ChatRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, session_id: str, role: str, content: str) -> None:
        self.db.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            [session_id, role, content, now_iso()],
        )

    def history(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            [session_id, limit],
        )
        return list(reversed(rows))


class CategoryRepo:
    def __init__(self, db: Database):
        self.db = db

    def sync(self, categories: list[dict[str, Any]]) -> None:
        ts = now_iso()
        with self.db.transaction() as conn:
            for item in categories:
                conn.execute(
                    """INSERT INTO categories (key, display_name, strategy, enabled, config_json, updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET display_name=excluded.display_name,
                       strategy=excluded.strategy, enabled=excluded.enabled,
                       config_json=excluded.config_json, updated_at=excluded.updated_at""",
                    [
                        item["key"],
                        item["name"],
                        item["strategy"],
                        1 if item.get("enabled", True) else 0,
                        _json(item),
                        ts,
                    ],
                )

    def list_enabled(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM categories WHERE enabled=1 ORDER BY key")

    def get(self, key: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM categories WHERE key=?", [key])


class BookingRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, booking: Booking) -> Booking:
        self.db.execute(
            """INSERT INTO bookings (id,user_id,category,title,slots_json,attributes_json,budget,
               status,window_start,window_end,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                booking.id,
                booking.user_id,
                booking.category,
                booking.title,
                _json(booking.slots),
                _json(booking.attributes),
                booking.budget,
                booking.status,
                booking.window_start,
                booking.window_end,
                booking.created_at,
                booking.updated_at,
            ],
        )
        return booking

    def get(self, booking_id: str, category: str | None = None) -> Booking | None:
        if category:
            row = self.db.query_one(
                "SELECT * FROM bookings WHERE id=? AND category=?", [booking_id, category]
            )
        else:
            row = self.db.query_one("SELECT * FROM bookings WHERE id=?", [booking_id])
        return Booking.from_row(row) if row else None

    def update(self, booking_id: str, **fields: Any) -> None:
        if "slots" in fields:
            fields["slots_json"] = _json(fields.pop("slots"))
        if "attributes" in fields:
            fields["attributes_json"] = _json(fields.pop("attributes"))
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(
            f"UPDATE bookings SET {columns} WHERE id=?", [*fields.values(), booking_id]
        )

    def list(self, user_id: str | None = None, limit: int = 50) -> list[Booking]:
        if user_id:
            rows = self.db.query(
                "SELECT * FROM bookings WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                [user_id, limit],
            )
        else:
            rows = self.db.query(
                "SELECT * FROM bookings ORDER BY created_at DESC LIMIT ?", [limit]
            )
        return [Booking.from_row(r) for r in rows]


class WatchRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        booking_id: str,
        category: str,
        interval_minutes: int,
        window_end: str,
        next_run_at: str,
    ) -> str:
        task_id = new_id("w")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE watch_tasks SET status='stopped', updated_at=? "
                "WHERE booking_id=? AND status='running'",
                [now_iso(), booking_id],
            )
            conn.execute(
                """INSERT INTO watch_tasks (id,booking_id,category,interval_minutes,window_end,
                   next_run_at,status,last_decision,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    task_id,
                    booking_id,
                    category,
                    interval_minutes,
                    window_end,
                    next_run_at,
                    "running",
                    "",
                    now_iso(),
                ],
            )
        return task_id

    def get_active(self, booking_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM watch_tasks WHERE booking_id=? AND status='running'", [booking_id]
        )

    def list_running(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM watch_tasks WHERE status='running'")

    def due(self, at: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM watch_tasks WHERE status='running' AND next_run_at<=? ORDER BY next_run_at",
            [at],
        )

    def update(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE watch_tasks SET {columns} WHERE id=?", [*fields.values(), task_id])

    def stop(self, booking_id: str) -> None:
        self.db.execute(
            "UPDATE watch_tasks SET status='stopped', updated_at=? "
            "WHERE booking_id=? AND status='running'",
            [now_iso(), booking_id],
        )


class PriceRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, snapshot: PriceSnapshot) -> None:
        snapshot.finalize()
        self.db.execute(
            """INSERT INTO price_snapshots
               (category,platform,item_id,item_name,price,tax,attributes_json,source,confidence,
                captured_at,stale,quarantined,hour_bucket)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(category,platform,item_id,hour_bucket) DO UPDATE SET
               price=excluded.price, tax=excluded.tax, attributes_json=excluded.attributes_json,
               source=excluded.source, confidence=excluded.confidence,
               captured_at=excluded.captured_at, stale=excluded.stale,
               quarantined=excluded.quarantined, item_name=excluded.item_name""",
            [
                snapshot.category,
                snapshot.platform,
                snapshot.item_id,
                snapshot.item_name,
                snapshot.price,
                snapshot.tax,
                _json(snapshot.attributes),
                snapshot.source,
                snapshot.confidence,
                snapshot.captured_at,
                1 if snapshot.stale else 0,
                1 if snapshot.quarantined else 0,
                snapshot.hour_bucket,
            ],
        )

    def upsert_many(self, snapshots: Iterable[PriceSnapshot]) -> None:
        for item in snapshots:
            self.upsert(item)

    def recent(
        self, category: str, item_id: str | None = None, limit: int = 200
    ) -> list[PriceSnapshot]:
        if item_id:
            rows = self.db.query(
                "SELECT * FROM price_snapshots WHERE category=? AND item_id=? "
                "ORDER BY captured_at DESC LIMIT ?",
                [category, item_id, limit],
            )
        else:
            rows = self.db.query(
                "SELECT * FROM price_snapshots WHERE category=? ORDER BY captured_at DESC LIMIT ?",
                [category, limit],
            )
        return _rows_to_prices(rows)

    def latest_by_platform_item(self, category: str) -> list[PriceSnapshot]:
        rows = self.db.query(
            """SELECT p.* FROM price_snapshots p
               JOIN (SELECT platform, item_id, MAX(captured_at) AS ts
                     FROM price_snapshots WHERE category=? GROUP BY platform, item_id) m
               ON p.platform=m.platform AND p.item_id=m.item_id AND p.captured_at=m.ts
               WHERE p.category=? AND p.quarantined=0
               ORDER BY p.price ASC""",
            [category, category],
        )
        return _rows_to_prices(rows)

    def latest_captured_at(self, category: str) -> str:
        row = self.db.query_one(
            "SELECT MAX(captured_at) AS ts FROM price_snapshots WHERE category=?", [category]
        )
        return (row or {}).get("ts") or ""

    def freshness_minutes(self, category: str) -> int:
        from ..models import parse_iso, now

        ts = parse_iso(self.latest_captured_at(category))
        if not ts:
            return 10**6
        return max(0, int((now() - ts).total_seconds() // 60))

    def set_quarantine(self, ids: list[int], quarantined: bool = True) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.db.execute(
            f"UPDATE price_snapshots SET quarantined=? WHERE id IN ({placeholders})",
            [1 if quarantined else 0, *ids],
        )


class CouponRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, coupon: Coupon) -> None:
        self.db.execute(
            """INSERT INTO coupons
               (category,platform,coupon_code,title,coupon_type,amount,discount_rate,min_spend,
                valid_from,valid_to,scope_json,source,confidence,status,collected_at,quarantined)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(category,platform,coupon_code) DO UPDATE SET
               title=excluded.title, coupon_type=excluded.coupon_type, amount=excluded.amount,
               discount_rate=excluded.discount_rate, min_spend=excluded.min_spend,
               valid_from=excluded.valid_from, valid_to=excluded.valid_to,
               scope_json=excluded.scope_json, source=excluded.source,
               confidence=excluded.confidence, status=excluded.status,
               collected_at=excluded.collected_at, quarantined=excluded.quarantined""",
            [
                coupon.category,
                coupon.platform,
                coupon.coupon_code,
                coupon.title,
                coupon.coupon_type,
                coupon.amount,
                coupon.discount_rate,
                coupon.min_spend,
                coupon.valid_from,
                coupon.valid_to,
                _json(coupon.scope),
                coupon.source,
                coupon.confidence,
                coupon.status,
                coupon.collected_at,
                1 if coupon.quarantined else 0,
            ],
        )

    def upsert_many(self, coupons: Iterable[Coupon]) -> None:
        for item in coupons:
            self.upsert(item)

    def active(self, category: str) -> list[Coupon]:
        rows = self.db.query(
            "SELECT * FROM coupons WHERE category=? AND status='active' AND quarantined=0",
            [category],
        )
        return [c for c in _rows_to_coupons(rows) if c.is_valid()]

    def all(self, category: str, limit: int = 500) -> list[Coupon]:
        rows = self.db.query(
            "SELECT * FROM coupons WHERE category=? ORDER BY collected_at DESC LIMIT ?",
            [category, limit],
        )
        return _rows_to_coupons(rows)

    def get(self, coupon_id: int | str) -> Coupon | None:
        row = self.db.query_one("SELECT * FROM coupons WHERE id=?", [coupon_id])
        return Coupon.from_row(row) if row else None

    def find_by_code(self, category: str, platform: str, code: str) -> Coupon | None:
        row = self.db.query_one(
            "SELECT * FROM coupons WHERE category=? AND platform=? AND coupon_code=?",
            [category, platform, code],
        )
        return Coupon.from_row(row) if row else None


class OfferRepo:
    def __init__(self, db: Database):
        self.db = db

    def replace(self, booking_id: str, category: str, offers: list[Offer]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM offers WHERE booking_id=? AND category=?", [booking_id, category])
            for offer in offers:
                conn.execute(
                    """INSERT INTO offers (id,booking_id,category,platform,item_id,item_name,price,
                       coupon_id,coupon_code,discount,paid_price,attributes_json,quoted_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        offer.id,
                        offer.booking_id,
                        offer.category,
                        offer.platform,
                        offer.item_id,
                        offer.item_name,
                        offer.price,
                        offer.coupon_id,
                        offer.coupon_code,
                        offer.discount,
                        offer.paid_price,
                        _json(offer.attributes),
                        offer.quoted_at,
                    ],
                )

    def list(self, booking_id: str, category: str | None = None) -> list[Offer]:
        if category:
            rows = self.db.query(
                "SELECT * FROM offers WHERE booking_id=? AND category=? ORDER BY paid_price ASC",
                [booking_id, category],
            )
        else:
            rows = self.db.query(
                "SELECT * FROM offers WHERE booking_id=? ORDER BY paid_price ASC", [booking_id]
            )
        return [self._from_row(r) for r in rows]

    def get(self, offer_id: str) -> Offer | None:
        row = self.db.query_one("SELECT * FROM offers WHERE id=?", [offer_id])
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Offer:
        import json

        return Offer(
            id=row["id"],
            booking_id=row["booking_id"],
            category=row["category"],
            platform=row["platform"],
            item_id=row["item_id"],
            item_name=row.get("item_name", ""),
            price=float(row["price"]),
            coupon_id=str(row["coupon_id"]) if row.get("coupon_id") else None,
            coupon_code=row.get("coupon_code"),
            discount=float(row.get("discount") or 0),
            paid_price=float(row["paid_price"]),
            attributes=json.loads(row.get("attributes_json") or "{}"),
            quoted_at=row.get("quoted_at", ""),
        )


class OrderRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, order: Order) -> Order:
        existing = self.find_by_idem(order.idempotency_key)
        if existing:
            return existing
        self.db.execute(
            """INSERT INTO orders (id,booking_id,category,platform,list_price,coupon_id,discount,
               paid_price,order_no,status,deep_link,idempotency_key,paid_at,created_at,item_id,
               item_name,attributes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                order.id,
                order.booking_id,
                order.category,
                order.platform,
                order.list_price,
                order.coupon_id,
                order.discount,
                order.paid_price,
                order.order_no,
                order.status,
                order.deep_link,
                order.idempotency_key,
                order.paid_at,
                order.created_at,
                order.item_id,
                order.item_name,
                _json(order.attributes),
            ],
        )
        return order

    def get(self, order_id: str) -> Order | None:
        row = self.db.query_one("SELECT * FROM orders WHERE id=?", [order_id])
        return Order.from_row(row) if row else None

    def find_by_idem(self, key: str) -> Order | None:
        row = self.db.query_one("SELECT * FROM orders WHERE idempotency_key=?", [key])
        return Order.from_row(row) if row else None

    def update(self, order_id: str, **fields: Any) -> None:
        columns = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE orders SET {columns} WHERE id=?", [*fields.values(), order_id])

    def list(self, booking_id: str | None = None, limit: int = 100) -> list[Order]:
        if booking_id:
            rows = self.db.query(
                "SELECT * FROM orders WHERE booking_id=? ORDER BY created_at DESC LIMIT ?",
                [booking_id, limit],
            )
        else:
            rows = self.db.query("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", [limit])
        return [Order.from_row(r) for r in rows]


class DecisionRepo:
    def __init__(self, db: Database):
        self.db = db

    def save(self, decision: Decision, predictions: list[ModelPrediction] | None = None) -> int:
        run_id = self.db.execute(
            """INSERT INTO decision_runs (booking_id,category,input_features_json,local_pred,
               aggregated_pred,decision_score,verdict,judge_used,low_diversity,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                decision.booking_id,
                decision.category,
                _json(decision.features),
                decision.local_prediction,
                decision.predicted_median,
                decision.decision_score,
                decision.recommendation,
                1 if decision.judge_used else 0,
                1 if decision.low_diversity else 0,
                _json(to_dict(decision)),
                decision.created_at,
            ],
        )
        for pred in predictions or []:
            self.db.execute(
                """INSERT INTO model_predictions (decision_run_id,category,provider,model,role,
                   prediction,direction,confidence,score,is_cold_start,cost_usd,payload_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    run_id,
                    decision.category,
                    pred.provider,
                    pred.model,
                    pred.role,
                    pred.prediction,
                    pred.direction,
                    pred.confidence,
                    pred.score,
                    1 if pred.is_cold_start else 0,
                    pred.cost_usd,
                    _json(to_dict(pred)),
                    now_iso(),
                ],
            )
        return run_id

    def latest(self, booking_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM decision_runs WHERE booking_id=? ORDER BY created_at DESC LIMIT 1",
            [booking_id],
        )

    def history(self, booking_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM decision_runs WHERE booking_id=? ORDER BY created_at DESC LIMIT ?",
            [booking_id, limit],
        )

    def predictions(self, run_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM model_predictions WHERE decision_run_id=? ORDER BY score DESC", [run_id]
        )

    def recent(self, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if category:
            return self.db.query(
                "SELECT * FROM decision_runs WHERE category=? ORDER BY created_at DESC LIMIT ?",
                [category, limit],
            )
        return self.db.query(
            "SELECT * FROM decision_runs ORDER BY created_at DESC LIMIT ?", [limit]
        )


class ModelStatsRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, category: str, provider: str, model: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM model_stats WHERE category=? AND provider=? AND model=?",
            [category, provider, model],
        )

    def upsert(
        self,
        category: str,
        provider: str,
        model: str,
        samples: int,
        mae: float,
        direction_accuracy: float,
        brier: float,
        reliability: float,
    ) -> None:
        self.db.execute(
            """INSERT INTO model_stats (category,provider,model,samples,mae,direction_accuracy,
               brier,reliability_score,updated_at) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(category,provider,model) DO UPDATE SET samples=excluded.samples,
               mae=excluded.mae, direction_accuracy=excluded.direction_accuracy,
               brier=excluded.brier, reliability_score=excluded.reliability_score,
               updated_at=excluded.updated_at""",
            [
                category,
                provider,
                model,
                samples,
                mae,
                direction_accuracy,
                brier,
                reliability,
                now_iso(),
            ],
        )


class NotificationRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, notification: Notification) -> str:
        existing = self.db.query_one(
            "SELECT id FROM notifications WHERE booking_id=? AND type=? AND window_key=?",
            [notification.booking_id, notification.type, notification.window_key],
        )
        if existing:
            return existing["id"]
        self.db.execute(
            """INSERT INTO notifications (id,user_id,booking_id,category,type,channel,payload_json,
               status,scheduled_at,sent_at,window_key,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                notification.id,
                notification.user_id,
                notification.booking_id,
                notification.category,
                notification.type,
                notification.channel,
                _json(notification.payload),
                notification.status,
                notification.scheduled_at,
                notification.sent_at,
                notification.window_key,
                notification.error,
            ],
        )
        return notification.id

    def mark(self, notification_id: str, status: str, error: str = "") -> None:
        self.db.execute(
            "UPDATE notifications SET status=?, sent_at=?, error=? WHERE id=?",
            [status, now_iso() if status == "sent" else "", error, notification_id],
        )

    def due(self, at: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM notifications WHERE status='pending' AND scheduled_at<=? "
            "ORDER BY scheduled_at",
            [at],
        )

    def failed(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM notifications WHERE status='failed' ORDER BY scheduled_at LIMIT ?",
            [limit],
        )

    def list(self, booking_id: str | None = None) -> list[dict[str, Any]]:
        if booking_id:
            return self.db.query(
                "SELECT * FROM notifications WHERE booking_id=? ORDER BY scheduled_at", [booking_id]
            )
        return self.db.query("SELECT * FROM notifications ORDER BY scheduled_at DESC LIMIT 100")


class CheckRepo:
    def __init__(self, db: Database):
        self.db = db

    def save(self, report: dict[str, Any]) -> int:
        return self.db.execute(
            """INSERT INTO check_reports (run_at,category,fatal_count,warn_count,summary,details_json)
               VALUES (?,?,?,?,?,?)""",
            [
                report["run_at"],
                report["category"],
                report["fatal_count"],
                report["warn_count"],
                report["summary"],
                _json(report.get("details", {})),
            ],
        )

    def latest(self, category: str | None = None) -> dict[str, Any] | None:
        if category:
            return self.db.query_one(
                "SELECT * FROM check_reports WHERE category=? ORDER BY run_at DESC LIMIT 1",
                [category],
            )
        return self.db.query_one("SELECT * FROM check_reports ORDER BY run_at DESC LIMIT 1")


class EventRepo:
    def __init__(self, db: Database):
        self.db = db

    def log(
        self,
        agent: str,
        event_type: str,
        payload: dict[str, Any],
        booking_id: str = "",
        category: str = "",
    ) -> None:
        self.db.execute(
            """INSERT INTO agent_events (booking_id,category,agent,event_type,payload_json,created_at)
               VALUES (?,?,?,?,?,?)""",
            [booking_id, category, agent, event_type, _json(payload), now_iso()],
        )

    def list(self, booking_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM agent_events WHERE booking_id=? ORDER BY created_at", [booking_id]
        )


class CredentialRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(
        self,
        user_id: str,
        platform: str,
        account_alias: str,
        auth_type: str = "qr_session",
        ciphertext: str = "",
        nonce: str = "",
        key_version: int = 1,
        expires_at: str = "",
    ) -> str:
        existing = self.db.query_one(
            "SELECT id FROM credentials WHERE user_id=? AND platform=?", [user_id, platform]
        )
        if existing:
            self.db.execute(
                """UPDATE credentials SET account_alias=?, auth_type=?, ciphertext=?, nonce=?,
                   key_version=?, expires_at=? WHERE id=?""",
                [account_alias, auth_type, ciphertext, nonce, key_version, expires_at, existing["id"]],
            )
            return existing["id"]
        cred_id = new_id("c")
        self.db.execute(
            """INSERT INTO credentials (id,user_id,platform,account_alias,auth_type,ciphertext,
               nonce,key_version,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                cred_id,
                user_id,
                platform,
                account_alias,
                auth_type,
                ciphertext,
                nonce,
                key_version,
                expires_at,
                now_iso(),
            ],
        )
        return cred_id

    def list(self, user_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM credentials WHERE user_id=? ORDER BY platform", [user_id]
        )

    def get(self, user_id: str, platform: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM credentials WHERE user_id=? AND platform=?", [user_id, platform]
        )


class PushRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, user_id: str, endpoint: str, p256dh: str, auth: str, ua: str = "") -> str:
        existing = self.db.query_one(
            "SELECT id FROM push_subscriptions WHERE endpoint=?", [endpoint]
        )
        if existing:
            self.db.execute(
                "UPDATE push_subscriptions SET user_id=?, p256dh=?, auth=?, ua=?, revoked_at='' "
                "WHERE id=?",
                [user_id, p256dh, auth, ua, existing["id"]],
            )
            return existing["id"]
        sub_id = new_id("p")
        self.db.execute(
            """INSERT INTO push_subscriptions (id,user_id,endpoint,p256dh,auth,ua,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            [sub_id, user_id, endpoint, p256dh, auth, ua, now_iso()],
        )
        return sub_id

    def remove(self, endpoint: str) -> None:
        self.db.execute(
            "UPDATE push_subscriptions SET revoked_at=? WHERE endpoint=?", [now_iso(), endpoint]
        )

    def list(self, user_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM push_subscriptions WHERE user_id=? AND revoked_at=''", [user_id]
        )


class RepositoryHub:
    def __init__(self, db: Database):
        self.db = db
        self.users = UserRepo(db)
        self.sessions = SessionRepo(db)
        self.chat = ChatRepo(db)
        self.categories = CategoryRepo(db)
        self.bookings = BookingRepo(db)
        self.watches = WatchRepo(db)
        self.prices = PriceRepo(db)
        self.coupons = CouponRepo(db)
        self.offers = OfferRepo(db)
        self.orders = OrderRepo(db)
        self.decisions = DecisionRepo(db)
        self.model_stats = ModelStatsRepo(db)
        self.notifications = NotificationRepo(db)
        self.checks = CheckRepo(db)
        self.events = EventRepo(db)
        self.credentials = CredentialRepo(db)
        self.push = PushRepo(db)
