"""通知智能体：Web Push / Webhook / 本地文件 / .ics 兜底。"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.logging import get_logger
from ..models import (
    Booking,
    Decision,
    Notification,
    NotificationSpec,
    Order,
    now,
    now_iso,
    new_id,
    to_dict,
)
from ..storage.repositories import RepositoryHub

logger = get_logger("better_buyer.notify")

ICS_TEMPLATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//better-buyer//CN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{stamp}
DTSTART:{start}
SUMMARY:{summary}
DESCRIPTION:{description}
END:VEVENT
END:VCALENDAR
"""


class NotificationAgent:
    def __init__(self, settings: Settings, repos: RepositoryHub, event_bus: Any = None):
        self.settings = settings
        self.repos = repos
        self.event_bus = event_bus
        self.offline = os.environ.get("BB_NOTIFY_MODE", "local").lower() != "webhook"

    # ------------------------------------------------------------------
    def schedule_plan(self, booking: Booking, specs: list[NotificationSpec]) -> list[str]:
        ids: list[str] = []
        for spec in specs:
            notification = Notification(
                id=new_id("n"),
                user_id=booking.user_id,
                booking_id=booking.id,
                category=booking.category,
                type=spec.type,
                channel=spec.channel,
                payload={"title": spec.title, "body": spec.body, "at": spec.at},
                scheduled_at=spec.at or now_iso(),
                window_key=spec.at or spec.type,
            )
            ids.append(self.repos.notifications.add(notification))
        return ids

    def decision_payload(self, booking: Booking, decision: Decision) -> dict[str, Any]:
        return {
            "event": "decision",
            "booking_id": booking.id,
            "category": booking.category,
            "recommendation": decision.recommendation,
            "decision_score": decision.decision_score,
            "confidence": decision.confidence,
            "effective_now": decision.current_effective_price,
            "predicted_median": decision.predicted_median,
            "optimal_window": list(decision.optimal_window),
            "reasons": decision.reasons,
            "model_votes": [vote.to_dict() for vote in decision.model_votes],
            "judge_used": decision.judge_used,
            "data_freshness_minutes": decision.data_freshness_minutes,
        }

    def notify_decision(self, booking: Booking, decision: Decision) -> dict[str, Any]:
        payload = self.decision_payload(booking, decision)
        notification = Notification(
            id=new_id("n"),
            user_id=booking.user_id,
            booking_id=booking.id,
            category=booking.category,
            type="decision_reminder",
            channel="web_push",
            payload=payload,
            scheduled_at=now_iso(),
            window_key=f"{decision.recommendation}:{decision.predicted_median}",
        )
        notification_id = self.repos.notifications.add(notification)
        self._deliver(notification_id)
        return payload

    # ------------------------------------------------------------------
    def process_due(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in self.repos.notifications.due(now_iso()):
            results.append(self._deliver(row["id"]))
        return results

    def retry_failed(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in self.repos.notifications.failed():
            results.append(self._deliver(row["id"]))
        return results

    def _deliver(self, notification_id: str) -> dict[str, Any]:
        row = self.repos.db.query_one("SELECT * FROM notifications WHERE id=?", [notification_id])
        if not row:
            return {"id": notification_id, "status": "missing"}
        payload = json.loads(row.get("payload_json") or "{}")
        channel, status, error = "local", "sent", ""

        if not self.offline and self.settings.notify_webhook:
            channel = "webhook"
            try:
                request = urllib.request.Request(
                    self.settings.notify_webhook,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"webhook status {resp.status}")
            except Exception as exc:  # 降级本地
                status, error = "failed", str(exc)
                logger.warning("Webhook 通知失败：%s", exc)
                channel = "webhook"
        else:
            self._write_local(notification_id, row, payload)

        if row.get("type") == "departure_reminder" or row.get("type") == "checkin_reminder":
            self._write_ics(notification_id, row, payload)

        self.repos.notifications.mark(notification_id, status, error)
        if status == "sent" and self.event_bus is not None:
            # 决策提醒由 WatchAgent 以更完整的 payload 发布，这里只推普通提醒
            if row.get("type") != "decision_reminder":
                self.event_bus.publish(
                    {
                        "event": "notification",
                        "title": payload.get("title") or "提醒",
                        "body": payload.get("body") or "",
                        "type": row.get("type"),
                        "booking_id": row.get("booking_id") or "",
                        "category": row.get("category") or "",
                    }
                )
        return {"id": notification_id, "status": status, "channel": channel, "error": error}

    def _write_local(self, notification_id: str, row: dict[str, Any], payload: dict[str, Any]) -> None:
        self.settings.notifications_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.notifications_dir / f"{notification_id}.json"
        body = {
            "id": notification_id,
            "type": row.get("type"),
            "category": row.get("category"),
            "scheduled_at": row.get("scheduled_at"),
            **payload,
        }
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_ics(self, notification_id: str, row: dict[str, Any], payload: dict[str, Any]) -> None:
        self.settings.notifications_dir.mkdir(parents=True, exist_ok=True)
        start = str(row.get("scheduled_at") or "").replace("-", "").replace(":", "").replace("+08:00", "")
        if not start:
            return
        stamp = now().strftime("%Y%m%dT%H%M%SZ")
        content = ICS_TEMPLATE.format(
            uid=f"{notification_id}@better-buyer",
            stamp=stamp,
            start=start,
            summary=payload.get("title", "提醒"),
            description=str(payload.get("body", "")).replace("\n", " "),
        )
        (self.settings.notifications_dir / f"{notification_id}.ics").write_text(
            content, encoding="utf-8"
        )

    # ------------------------------------------------------------------
    def schedule_order_notifications(self, order: Order, booking: Booking, strategy: Any) -> list[str]:
        specs = strategy.notification_plan(booking)
        return self.schedule_plan(booking, specs)
