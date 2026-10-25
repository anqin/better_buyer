"""自定义品类示例：新增品类只需配置 + 一个策略类。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..models import (
    CategoryQuery,
    Issue,
    NotificationSpec,
    PriceSnapshot,
    PurchaseIntent,
    SlotSpec,
)
from .base import CategoryStrategy


class CustomExampleCategory(CategoryStrategy):
    key = "custom_example"
    display_name = "示例品类"
    coupon_scope = "自定义"
    slot_specs = {
        "item_id": SlotSpec("item_id", "商品", True, "str", "请问要买什么商品？"),
        "quantity": SlotSpec("quantity", "数量", False, "int", "需要几件？"),
        "budget": SlotSpec("budget", "预算", False, "float", "预算大概多少？"),
    }

    def normalize_slots(self, raw: dict[str, Any]) -> dict[str, Any]:
        slots = super().normalize_slots(raw)
        slots["quantity"] = int(slots.get("quantity") or 1)
        return slots

    def validate_slots(self, slots: dict[str, Any]) -> list[Issue]:
        issues = super().validate_slots(slots)
        if slots.get("quantity") and int(slots["quantity"]) <= 0:
            issues.append(Issue("fatal", "INVALID_QUANTITY", "数量必须大于 0", "quantity"))
        return issues

    def title(self, intent: PurchaseIntent) -> str:
        return f"{intent.slots.get('item_id', '')} × {intent.slots.get('quantity', 1)}"

    def build_query(self, intent: PurchaseIntent) -> CategoryQuery:
        return CategoryQuery(
            self.key,
            {
                "item_id": intent.slots.get("item_id"),
                "quantity": int(intent.slots.get("quantity") or 1),
            },
        )

    def build_deep_link(self, offer: Any) -> str:
        attrs = getattr(offer, "attributes", {}) or {}
        keyword = attrs.get("item_name") or attrs.get("item_id") or ""
        coupon = getattr(offer, "coupon_code", "") or ""
        return f"https://s.taobao.com/search?{urlencode({'q': f'{keyword} {coupon}'.strip()})}"

    def build_prediction_features(
        self,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list,
        at=None,
    ) -> dict[str, Any]:
        features = super().build_prediction_features(intent, snapshots, coupons, at)
        features["item_id"] = intent.slots.get("item_id")
        features["quantity"] = int(intent.slots.get("quantity") or 1)
        return features

    def notification_plan(self, booking: Any) -> list[NotificationSpec]:
        return []
