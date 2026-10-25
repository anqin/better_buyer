"""后台调度器：盯盘、通知、定时采券、每日质检与校准。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from ..agents.checker import DataChecker, daily_run_hour
from ..agents.collector import CollectionAgent
from ..agents.watcher import WatchAgent
from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import get_logger
from ..models import now, now_iso
from ..services.calibration import CalibrationService
from ..services.notification import NotificationAgent
from ..storage.repositories import RepositoryHub

logger = get_logger("better_buyer.scheduler")


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        repos: RepositoryHub,
        collector: CollectionAgent,
        watcher: WatchAgent,
        notifier: NotificationAgent,
        checker: DataChecker,
        calibration: CalibrationService,
        event_bus: EventBus | None = None,
        interval_seconds: int = 15,
    ):
        self.settings = settings
        self.repos = repos
        self.collector = collector
        self.watcher = watcher
        self.notifier = notifier
        self.checker = checker
        self.calibration = calibration
        self.event_bus = event_bus
        self.interval_seconds = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_check_date = ""
        self._last_coupon_slot = ""
        self._last_calibration_date = ""
        self._last_tick = ""
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="better-buyer-scheduler", daemon=True)
        self._thread.start()
        logger.info("后台调度器已启动，间隔 %s 秒", self.interval_seconds)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("后台调度器已停止")

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 调度循环必须自愈
                self._errors.append(f"{now_iso()} {exc}")
                self._errors = self._errors[-20:]
                logger.warning("调度 tick 失败：%s", exc)
            self._stop.wait(self.interval_seconds)

    def tick(self) -> dict[str, Any]:
        result: dict[str, Any] = {"at": now_iso(), "notifications": 0, "watch": 0, "check": False}
        delivered = self.notifier.process_due()
        result["notifications"] = len(delivered)

        due = self.repos.watches.due(now_iso())
        if due:
            tasks = asyncio.run(self.watcher.run_due())
            result["watch"] = len(tasks)

        current = now()
        today = current.date().isoformat()
        if current.hour >= daily_run_hour(self.settings) and self._last_check_date != today:
            report = self.checker.run()
            self._last_check_date = today
            result["check"] = True
            result["check_fatal"] = report["fatal_count"]
            result["check_warn"] = report["warn_count"]
            if self.event_bus is not None:
                self.event_bus.publish(
                    {
                        "event": "check",
                        "fatal": report["fatal_count"],
                        "warn": report["warn_count"],
                    }
                )

        coupon_hours = _cron_hours(self.settings.collection.get("coupons_cron", "0 8,20 * * *"))
        slot = f"{today}T{current.hour}"
        if current.hour in coupon_hours and self._last_coupon_slot != slot:
            counts = self.collector.collect_all_coupons()
            self._last_coupon_slot = slot
            result["coupons"] = counts
            if self.event_bus is not None:
                self.event_bus.publish({"event": "coupons", "counts": counts})

        if current.hour == 3 and self._last_calibration_date != today:
            result["calibration"] = self.calibration.run()
            self._last_calibration_date = today

        self._last_tick = result["at"]
        return result

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "last_tick": self._last_tick,
            "running_watches": len(self.repos.watches.list_running()),
            "pending_notifications": len(self.repos.notifications.due(now_iso())),
            "last_check_date": self._last_check_date,
            "errors": self._errors[-5:],
        }


def _cron_hours(expr: str) -> set[int]:
    parts = str(expr).split()
    if len(parts) < 2:
        return {8, 20}
    hours: set[int] = set()
    for item in parts[1].split(","):
        try:
            hours.add(int(item))
        except ValueError:
            continue
    return hours or {8, 20}
