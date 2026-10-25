"""品类策略基类：槽位、券过滤、排序、特征、深链与通知。"""

from __future__ import annotations

import statistics
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

from ..core.datetime_utils import parse_date_text, today
from ..models import (
    CategoryQuery,
    Coupon,
    Issue,
    NotificationSpec,
    PriceSnapshot,
    PurchaseIntent,
    SlotSpec,
    iso,
    now,
    parse_iso,
)


class CategoryStrategy(ABC):
    """品类差异的唯一实现点；主智能体只面向通用的意图、快照与方案。"""

    key: str = ""
    display_name: str = ""
    coupon_scope: str = ""
    slot_specs: dict[str, SlotSpec] = {}
    buy_score_threshold: float = 0.60
    urgency_days: int = 3
    min_saving_margin: float = 0.03
    ranking_primary: str = "paid_price"
    require_in_stock: bool = False

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self.apply_config(self.config)

    # ------------------------------------------------------------------
    def apply_config(self, config: dict[str, Any]) -> None:
        self.key = config.get("key", self.key)
        self.display_name = config.get("name", self.display_name)
        self.coupon_scope = config.get("coupon_scope", self.coupon_scope)
        self.buy_score_threshold = float(
            config.get("buy_score_threshold", self.buy_score_threshold)
        )
        prediction = config.get("prediction") or {}
        self.urgency_days = int(prediction.get("urgency_days", self.urgency_days))
        self.min_saving_margin = float(
            prediction.get("min_saving_margin", self.min_saving_margin)
        )
        ranking = config.get("ranking") or {}
        self.ranking_primary = ranking.get("primary", self.ranking_primary)
        self.require_in_stock = bool(ranking.get("require_in_stock", self.require_in_stock))
        slots = config.get("slots") or {}
        required = set(slots.get("required") or [])
        optional = set(slots.get("optional") or [])
        for key, spec in self.slot_specs.items():
            if key in required:
                spec.required = True
            elif key in optional:
                spec.required = False

    # ------------------------------------------------------------------
    @property
    def required_slots(self) -> list[str]:
        return [key for key, spec in self.slot_specs.items() if spec.required]

    def slot_schema(self) -> dict[str, Any]:
        return {
            "category": self.key,
            "display_name": self.display_name,
            "slots": {key: spec.describe() for key, spec in self.slot_specs.items()},
            "required": self.required_slots,
            "buy_score_threshold": self.buy_score_threshold,
        }

    def normalize_slots(self, raw: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, spec in self.slot_specs.items():
            if key not in raw:
                continue
            value = raw[key]
            if value is None or value == "":
                continue
            if spec.type == "date":
                parsed = parse_date_text(str(value))
                normalized[key] = parsed.isoformat() if parsed else str(value)
            elif spec.type == "float":
                try:
                    normalized[key] = float(value)
                except (TypeError, ValueError):
                    normalized[key] = value
            elif spec.type == "int":
                try:
                    normalized[key] = int(value)
                except (TypeError, ValueError):
                    normalized[key] = value
            else:
                normalized[key] = str(value).strip()
        return normalized

    def validate_slots(self, slots: dict[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        for key in self.required_slots:
            if not slots.get(key):
                issues.append(Issue("warn", "MISSING_SLOT", f"缺少槽位：{key}", key))
        for key, spec in self.slot_specs.items():
            if spec.type == "date" and slots.get(key):
                parsed = parse_iso(str(slots[key]))
                if not parsed:
                    issues.append(Issue("fatal", "INVALID_DATE", f"{spec.title}格式非法", key))
                elif parsed.date() < today():
                    issues.append(Issue("fatal", "DATE_IN_PAST", f"{spec.title}早于今天", key))
        return issues

    def title(self, intent: PurchaseIntent) -> str:
        return self.display_name

    @abstractmethod
    def build_query(self, intent: PurchaseIntent) -> CategoryQuery: ...

    def coupon_scope_ok(self, coupon: Coupon) -> bool:
        scopes = coupon.scope.get("categories") or coupon.scope.get("scope") or []
        if isinstance(scopes, str):
            scopes = [scopes]
        accepted = {self.coupon_scope, self.key, "全品类", "*"}
        return any(item in accepted for item in scopes)

    def filter_coupon(self, coupon: Coupon, intent: PurchaseIntent) -> bool:
        if coupon.category != self.key:
            return False
        if not coupon.is_valid():
            return False
        return self.coupon_scope_ok(coupon)

    def rank_key(self, offer: Any, preferences: dict[str, Any] | None = None) -> tuple:
        preferences = preferences or {}
        preferred_platforms = preferences.get("preferred_platforms") or []
        platform_rank = (
            preferred_platforms.index(offer.platform)
            if offer.platform in preferred_platforms
            else len(preferred_platforms)
        )
        return (
            0 if getattr(offer, "in_stock", True) else 1,
            round(float(offer.paid_price), 2),
            platform_rank,
            -round(float(offer.discount or 0), 2),
            -round(float(getattr(offer, "confidence", 0.8)), 2),
        )

    # ------------------------------------------------------------------
    def _stats(self, snapshots: list[PriceSnapshot]) -> dict[str, Any]:
        prices = [s.total_price for s in snapshots if not s.quarantined and s.total_price > 0]
        if not prices:
            return {
                "sample_count": 0,
                "latest_price": 0.0,
                "avg_price": 0.0,
                "min_price": 0.0,
                "max_price": 0.0,
                "ewma": 0.0,
                "volatility": 0.0,
                "trend": 0.0,
                "price_series": [],
            }
        # 快照按时间倒序传入，计算时正序
        series = list(reversed(prices))
        ewma = series[0]
        for value in series[1:]:
            ewma = 0.65 * value + 0.35 * ewma
        if len(series) >= 2:
            n = len(series)
            mean_x = (n - 1) / 2
            mean_y = statistics.fmean(series)
            denom = sum((i - mean_x) ** 2 for i in range(n)) or 1
            trend = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(series)) / denom
        else:
            trend = 0.0
        volatility = (
            statistics.pstdev(series) / statistics.fmean(series) if len(series) > 1 else 0.0
        )
        return {
            "sample_count": len(series),
            "latest_price": round(series[-1], 2),
            "avg_price": round(statistics.fmean(series), 2),
            "min_price": round(min(series), 2),
            "max_price": round(max(series), 2),
            "ewma": round(ewma, 2),
            "volatility": round(volatility, 4),
            "trend": round(trend, 3),
            "price_series": [round(v, 2) for v in series[-30:]],
        }

    def build_prediction_features(
        self,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        at = at or now()
        features = {
            "category": self.key,
            "slots": dict(intent.slots),
            "budget": intent.budget,
            "stats": self._stats(snapshots),
            "active_coupons": [
                {
                    "platform": c.platform,
                    "code": c.coupon_code,
                    "amount": c.amount,
                    "type": c.coupon_type,
                    "min_spend": c.min_spend,
                    "valid_to": c.valid_to,
                }
                for c in coupons
                if c.is_valid(at)
            ],
            "hours_to_target": None,
        }
        return features

    def build_prediction_prompt(self, features: dict[str, Any]) -> str:
        return (
            "你是价格预测器。仅根据下列结构化统计输出 JSON，不要编造数据：\n"
            f"{features}\n"
            '返回 JSON：{"predicted_price": 数值, "direction": "down|flat|up", '
            '"confidence": 0-1, "reasons": ["..."], "evidence": ["..."]}'
        )

    def optimal_window(self, slots: dict[str, Any], at: datetime | None = None) -> tuple[str, str]:
        at = at or now()
        target = parse_iso(str(slots.get("depart_date") or slots.get("checkin_date") or ""))
        if not target:
            target = at
        start = max(at.date(), target.date() - timedelta(days=7))
        end = target.date()
        return start.isoformat(), end.isoformat()

    # ------------------------------------------------------------------
    @abstractmethod
    def build_deep_link(self, offer: Any) -> str: ...

    def notification_plan(self, booking: Any) -> list[NotificationSpec]:
        return []

    def validate_quote(self, quote: dict[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        price = float(quote.get("price") or 0)
        paid = float(quote.get("paid_price") or 0)
        if price <= 0:
            issues.append(Issue("fatal", "INVALID_PRICE", "价格必须大于 0", "price"))
        if paid < 0:
            issues.append(Issue("fatal", "INVALID_PAID_PRICE", "实付价不能为负", "paid_price"))
        return issues

    def is_urgent(
        self,
        intent: PurchaseIntent,
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> bool:
        at = at or now()
        target_key = "depart_date" if "depart_date" in intent.slots else "checkin_date"
        target = parse_iso(str(intent.slots.get(target_key) or ""))
        if not target:
            return False
        days = (target.date() - at.date()).days
        if days > self.urgency_days:
            return False
        soon = any(
            c.is_valid(at)
            and c.valid_to
            and (parse_iso(c.valid_to) or at).timestamp() - at.timestamp() <= 48 * 3600
            for c in coupons
        )
        return 0 <= days <= self.urgency_days or soon

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "coupon_scope": self.coupon_scope,
            "required_slots": self.required_slots,
            "buy_score_threshold": self.buy_score_threshold,
        }
