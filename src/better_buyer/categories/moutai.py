"""茅台品类策略。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ..models import (
    CategoryQuery,
    Coupon,
    Issue,
    NotificationSpec,
    PriceSnapshot,
    PurchaseIntent,
    SlotSpec,
    now,
)
from .base import CategoryStrategy


class MoutaiCategory(CategoryStrategy):
    key = "moutai"
    display_name = "茅台"
    coupon_scope = "白酒"
    slot_specs = {
        "sku": SlotSpec("sku", "SKU", True, "str", "请问要哪款茅台？例如：飞天茅台53度500ml"),
        "quantity": SlotSpec("quantity", "数量", False, "int", "需要几瓶？"),
        "spec": SlotSpec("spec", "规格", False, "str", "有规格要求吗？例如：500ml 单瓶/整箱"),
        "target_unit_price": SlotSpec("target_unit_price", "心理价", False, "float", "心理价位是多少（每瓶）？"),
        "channels": SlotSpec("channels", "渠道", False, "str", "偏好哪些渠道？"),
        "budget": SlotSpec("budget", "预算", False, "float", "总预算大概多少？"),
    }
    urgency_days = 2
    min_saving_margin = 0.0
    rank_primary = "unit_paid_price"
    require_in_stock = True

    def normalize_slots(self, raw: dict[str, Any]) -> dict[str, Any]:
        slots = super().normalize_slots(raw)
        if not slots.get("sku"):
            text = str(raw.get("text") or "")
            if "飞天" in text:
                slots["sku"] = "飞天茅台53度500ml"
        slots["quantity"] = int(slots.get("quantity") or raw.get("quantity") or 1)
        return slots

    def validate_slots(self, slots: dict[str, Any]) -> list[Issue]:
        issues = super().validate_slots(slots)
        quantity = slots.get("quantity") or 1
        try:
            if int(quantity) <= 0:
                issues.append(Issue("fatal", "INVALID_QUANTITY", "数量必须大于 0", "quantity"))
        except (TypeError, ValueError):
            issues.append(Issue("fatal", "INVALID_QUANTITY", "数量格式非法", "quantity"))
        target = slots.get("target_unit_price")
        if target is not None:
            try:
                if float(target) <= 0:
                    issues.append(Issue("fatal", "INVALID_TARGET", "心理价必须大于 0", "target_unit_price"))
            except (TypeError, ValueError):
                issues.append(Issue("fatal", "INVALID_TARGET", "心理价格式非法", "target_unit_price"))
        return issues

    def title(self, intent: PurchaseIntent) -> str:
        return f"{intent.slots.get('sku', '茅台')} × {intent.slots.get('quantity', 1)}"

    def build_query(self, intent: PurchaseIntent) -> CategoryQuery:
        return CategoryQuery(
            self.key,
            {
                "sku": intent.slots.get("sku"),
                "quantity": int(intent.slots.get("quantity") or 1),
                "spec": intent.slots.get("spec"),
                "channels": intent.slots.get("channels"),
            },
        )

    def filter_coupon(self, coupon: Coupon, intent: PurchaseIntent) -> bool:
        if not super().filter_coupon(coupon, intent):
            return False
        skus = coupon.scope.get("skus") or []
        sku = str(intent.slots.get("sku") or "")
        if skus and not any(token in sku for token in skus):
            return False
        return True

    def rank_key(self, offer: Any, preferences: dict[str, Any] | None = None) -> tuple:
        base = super().rank_key(offer, preferences)
        return base

    def build_deep_link(self, offer: Any) -> str:
        attrs = getattr(offer, "attributes", {}) or {}
        sku = attrs.get("sku") or "飞天茅台"
        coupon_code = getattr(offer, "coupon_code", "") or ""
        platform = getattr(offer, "platform", "")
        keyword = f"{sku} {coupon_code}".strip()
        if platform == "jd":
            return f"https://search.jd.com/Search?{urlencode({'keyword': keyword})}"
        if platform == "taobao":
            return f"https://s.taobao.com/search?{urlencode({'q': keyword})}"
        if platform == "douyin":
            return f"https://www.douyin.com/search/?{urlencode({'keyword': keyword})}"
        if platform == "ctrip":
            return f"https://www.ctrip.com/?{urlencode({'keyword': keyword})}"
        return f"https://www.fliggy.com/search?{urlencode({'keyword': keyword})}"

    def build_prediction_features(
        self,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        features = super().build_prediction_features(intent, snapshots, coupons, at)
        in_stock = [s for s in snapshots if s.attributes.get("in_stock", True)]
        features.update(
            {
                "sku": intent.slots.get("sku"),
                "quantity": int(intent.slots.get("quantity") or 1),
                "target_unit_price": intent.slots.get("target_unit_price"),
                "in_stock_platforms": sorted({s.platform for s in in_stock}),
                "stock_signal": "in_stock" if in_stock else "out_of_stock",
            }
        )
        return features

    def notification_plan(self, booking: Any) -> list[NotificationSpec]:
        return [
            NotificationSpec(
                type="restock_reminder",
                title="茅台补货提醒",
                body="关注的 SKU 已出现可购买库存，请尽快确认。",
                at=now().isoformat(timespec="seconds"),
            ),
            NotificationSpec(
                type="shipping_reminder",
                title="发货提醒",
                body="订单已进入发货流程，请留意物流信息。",
                at=now().isoformat(timespec="seconds"),
            ),
        ]

    def validate_quote(self, quote: dict[str, Any]) -> list[Issue]:
        issues = super().validate_quote(quote)
        if quote.get("in_stock") is False:
            issues.append(Issue("warn", "OUT_OF_STOCK", "该渠道暂时无货", "in_stock"))
        return issues

    def is_urgent(
        self,
        intent: PurchaseIntent,
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> bool:
        target = intent.slots.get("target_unit_price")
        if target is None:
            return False
        for coupon in coupons:
            if not self.filter_coupon(coupon, intent):
                continue
            if coupon.valid_to and coupon.valid_to[:10] <= now().date().isoformat():
                return True
        return False
