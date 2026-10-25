"""盯盘子智能体：周期采集、决策、推送与任务续排。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..core.logging import get_logger
from ..models import Booking, Decision, iso, now
from ..services.notification import NotificationAgent
from ..storage.repositories import RepositoryHub
from .collector import CollectionAgent
from .predictor import PredictorAgent

logger = get_logger("better_buyer.watcher")


class WatchAgent:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        repos: RepositoryHub,
        collector: CollectionAgent,
        predictor: PredictorAgent,
        notifier: NotificationAgent,
        event_bus: Any = None,
    ):
        self.settings = settings
        self.registry = registry
        self.repos = repos
        self.collector = collector
        self.predictor = predictor
        self.notifier = notifier
        self.event_bus = event_bus

    # ------------------------------------------------------------------
    def start(self, booking: Booking) -> str:
        existing = self.repos.watches.get_active(booking.id)
        if existing:
            return existing["id"]
        strategy = self.registry.get(booking.category)
        window = strategy.optimal_window(booking.slots)
        window_end = window[1]
        interval = self.settings.watch_interval_minutes
        task_id = self.repos.watches.create(
            booking_id=booking.id,
            category=booking.category,
            interval_minutes=interval,
            window_end=window_end,
            next_run_at=now().isoformat(timespec="seconds"),
        )
        self.repos.bookings.update(booking.id, status="watching")
        self.repos.events.log(
            "watcher",
            "start",
            {"interval_minutes": interval, "window_end": window_end},
            booking_id=booking.id,
            category=booking.category,
        )
        return task_id

    def stop(self, booking_id: str) -> None:
        self.repos.watches.stop(booking_id)
        self.repos.events.log("watcher", "stop", {}, booking_id=booking_id)

    def status(self, booking_id: str) -> dict[str, Any] | None:
        return self.repos.watches.get_active(booking_id)

    # ------------------------------------------------------------------
    async def run_due(self, at: datetime | None = None) -> list[dict[str, Any]]:
        at = at or now()
        results: list[dict[str, Any]] = []
        for task in self.repos.watches.due(iso(at)):
            try:
                results.append(await self.run_task(task))
            except Exception as exc:  # 单个任务失败不影响其他任务
                logger.warning("盯盘任务 %s 失败：%s", task.get("id"), exc)
                self.repos.watches.update(
                    task["id"],
                    next_run_at=iso(now() + timedelta(minutes=self.settings.watch_interval_minutes)),
                )
                results.append({"task_id": task.get("id"), "error": str(exc)})
        return results

    def run_due_sync(self) -> list[dict[str, Any]]:
        return asyncio.run(self.run_due())

    # ------------------------------------------------------------------
    async def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        booking = self.repos.bookings.get(task["booking_id"], task["category"])
        if booking is None or booking.status in ("completed", "cancelled"):
            self.repos.watches.stop(task["booking_id"])
            return {"task_id": task["id"], "status": "stopped"}
        window_end = _parse_date(task.get("window_end", ""))
        if window_end and window_end.date() < now().date():
            self.repos.watches.stop(booking.id)
            return {"task_id": task["id"], "status": "expired"}

        intent = self.predictor.intent_from_booking(booking)
        self.collector.collect(intent)
        decision = await self.predictor.run(booking, intent)
        next_run = now() + timedelta(minutes=int(task.get("interval_minutes") or 30))
        self.repos.watches.update(
            task["id"],
            next_run_at=iso(next_run),
            last_decision=decision.recommendation,
        )

        notified = False
        if self._should_notify(task, decision):
            self.notifier.notify_decision(booking, decision)
            notified = True

        event = {
            "event": "decision",
            "booking_id": booking.id,
            "category": booking.category,
            "decision": decision.to_dict(),
            "notified": notified,
        }
        self._publish(event)
        return {"task_id": task["id"], "decision": decision.to_dict(), "notified": notified}

    # ------------------------------------------------------------------
    @staticmethod
    def _should_notify(task: dict[str, Any], decision: Decision) -> bool:
        previous = str(task.get("last_decision") or "")
        if decision.recommendation in ("buy_now", "no_buy", "no_go"):
            return previous != decision.recommendation
        return False

    def _publish(self, event: dict[str, Any]) -> None:
        bus = self.event_bus
        if bus is None:
            return
        try:
            bus.publish(event)
        except Exception as exc:
            logger.warning("事件推送失败：%s", exc)


def _parse_date(value: str) -> datetime | None:
    from ..models import parse_iso

    return parse_iso(value)
