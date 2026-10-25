"""数据检查智能体：完整性与一致性检查，每日凌晨运行。"""

from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Any

from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..core.logging import get_logger
from ..models import now, now_iso, parse_iso
from ..storage.repositories import RepositoryHub

logger = get_logger("better_buyer.checker")


class DataChecker:
    def __init__(self, settings: Settings, registry: CategoryRegistry, repos: RepositoryHub):
        self.settings = settings
        self.registry = registry
        self.repos = repos
        self.outlier_ratio = float(settings.check_config.get("price_outlier_ratio", 0.4))

    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        reports: list[dict[str, Any]] = []
        for strategy in self.registry.enabled():
            reports.append(self.check_category(strategy.key))
        self.repos.events.log(
            "checker",
            "run",
            {
                "categories": [item["category"] for item in reports],
                "fatal": sum(item["fatal_count"] for item in reports),
                "warn": sum(item["warn_count"] for item in reports),
            },
        )
        return {
            "run_at": now_iso(),
            "reports": reports,
            "fatal_count": sum(item["fatal_count"] for item in reports),
            "warn_count": sum(item["warn_count"] for item in reports),
        }

    # ------------------------------------------------------------------
    def check_category(self, category: str) -> dict[str, Any]:
        strategy = self.registry.get(category)
        fatal: list[dict[str, Any]] = []
        warn: list[dict[str, Any]] = []
        fixed: list[str] = []

        prices = self.repos.prices.recent(category, limit=1000)
        if not prices:
            warn.append({"code": "NO_PRICE_DATA", "message": "暂无价格快照"})
        valid_prices: list[float] = []
        for snapshot in prices:
            if snapshot.price <= 0 or snapshot.total_price <= 0:
                fatal.append(
                    {
                        "code": "INVALID_PRICE",
                        "message": f"{snapshot.platform}/{snapshot.item_id} 价格非法",
                        "captured_at": snapshot.captured_at,
                    }
                )
                continue
            if not snapshot.item_id:
                fatal.append({"code": "MISSING_ITEM_ID", "message": f"{snapshot.platform} 缺少标的 ID"})
                continue
            if not parse_iso(snapshot.captured_at):
                fatal.append({"code": "INVALID_TIMESTAMP", "message": f"{snapshot.platform} 采集时间非法"})
                continue
            valid_prices.append(snapshot.total_price)

        stale_minutes = self.repos.prices.freshness_minutes(category)
        if prices and stale_minutes > int(self.settings.price_stale_hours * 60):
            warn.append(
                {
                    "code": "STALE_PRICE",
                    "message": f"最新价格已 {stale_minutes} 分钟未更新",
                }
            )

        if len(valid_prices) >= 3:
            median = statistics.median(valid_prices)
            outliers = [p for p in valid_prices if abs(p - median) / max(median, 1) > self.outlier_ratio]
            if outliers:
                warn.append(
                    {
                        "code": "PRICE_OUTLIER",
                        "message": f"发现 {len(outliers)} 条价格偏离中位数超过 {self.outlier_ratio:.0%}",
                        "samples": [round(p, 2) for p in outliers[:5]],
                    }
                )

        coupons = self.repos.coupons.all(category)
        if not coupons:
            warn.append({"code": "NO_COUPON_DATA", "message": "暂无优惠券数据"})
        expired_ids = 0
        for coupon in coupons:
            if not coupon.coupon_code:
                fatal.append({"code": "MISSING_COUPON_CODE", "message": f"{coupon.platform} 缺少券码"})
            if coupon.valid_from and coupon.valid_to:
                start = parse_iso(coupon.valid_from)
                end = parse_iso(coupon.valid_to)
                if start and end and end < start:
                    fatal.append(
                        {
                            "code": "INVALID_COUPON_RANGE",
                            "message": f"{coupon.platform}/{coupon.coupon_code} 有效期颠倒",
                        }
                    )
            if coupon.coupon_type == "amount" and coupon.amount <= 0:
                warn.append(
                    {
                        "code": "ZERO_COUPON_AMOUNT",
                        "message": f"{coupon.platform}/{coupon.coupon_code} 金额为 0",
                    }
                )
            if coupon.valid_to and (parse_iso(coupon.valid_to) or now()) < now():
                if coupon.status != "expired":
                    expired_ids += 1
        if expired_ids:
            self.repos.db.execute(
                "UPDATE coupons SET status='expired' WHERE category=? AND valid_to<>'' AND valid_to<?",
                [category, now().isoformat(timespec="seconds")],
            )
            fixed.append(f"标记 {expired_ids} 张过期券")

        bookings = self.repos.bookings.list(limit=200)
        for booking in bookings:
            if booking.category != category:
                continue
            missing = [key for key in strategy.required_slots if not booking.slots.get(key)]
            if missing:
                warn.append(
                    {
                        "code": "BOOKING_MISSING_SLOT",
                        "message": f"预约 {booking.id} 缺少槽位 {missing}",
                    }
                )
            if booking.budget is not None and booking.budget <= 0:
                fatal.append(
                    {"code": "INVALID_BUDGET", "message": f"预约 {booking.id} 预算非法"}
                )
            if booking.window_end and (parse_iso(booking.window_end) or now()) < now():
                if booking.status in ("collecting", "watching"):
                    warn.append(
                        {
                            "code": "EXPIRED_WINDOW",
                            "message": f"预约 {booking.id} 已过目标日期但仍在 {booking.status}",
                        }
                    )

        orders = self.repos.orders.list(limit=200)
        for order in orders:
            if order.category != category:
                continue
            expected = round(order.list_price - order.discount, 2)
            if abs(expected - order.paid_price) > 0.01:
                fatal.append(
                    {
                        "code": "ORDER_PRICE_MISMATCH",
                        "message": f"订单 {order.id} 实付与标价-优惠不一致",
                    }
                )
            if order.status == "paid" and not order.paid_at:
                fatal.append(
                    {"code": "PAID_WITHOUT_TIME", "message": f"订单 {order.id} 已支付但缺少支付时间"}
                )

        if fatal:
            self.repos.db.execute(
                "UPDATE price_snapshots SET quarantined=1 WHERE category=? AND (price<=0 OR item_id='')",
                [category],
            )
            fixed.append("隔离非法价格快照")

        summary = (
            f"{strategy.display_name}：致命 {len(fatal)}，警告 {len(warn)}"
            + (f"，已修复：{'；'.join(fixed)}" if fixed else "")
        )
        report = {
            "run_at": now_iso(),
            "category": category,
            "fatal_count": len(fatal),
            "warn_count": len(warn),
            "summary": summary,
            "details": {"fatal": fatal, "warn": warn, "fixed": fixed},
        }
        self.repos.checks.save(report)
        logger.info(summary)
        return report


def daily_run_hour(settings: Settings) -> int:
    """从 cron 字符串解析小时；默认凌晨 1 点。"""
    cron = str(settings.check_config.get("cron", "0 1 * * *")).split()
    if len(cron) >= 2:
        try:
            return int(cron[1])
        except ValueError:
            return 1
    return 1


def next_daily_run(settings: Settings, at=None) -> str:
    at = at or now()
    hour = daily_run_hour(settings)
    target = at.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= at:
        target += timedelta(days=1)
    return target.isoformat(timespec="seconds")
