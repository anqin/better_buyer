"""酒店品类策略。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
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
    iso,
    now,
    parse_iso,
)
from .base import CategoryStrategy


class HotelCategory(CategoryStrategy):
    key = "hotel"
    display_name = "酒店"
    coupon_scope = "酒店"
    slot_specs = {
        "city": SlotSpec("city", "城市", True, "str", "请问要住哪个城市？"),
        "checkin_date": SlotSpec("checkin_date", "入住日期", True, "date", "请问哪天入住？"),
        "checkout_date": SlotSpec("checkout_date", "离店日期", True, "date", "请问哪天离店？"),
        "star_level": SlotSpec("star_level", "星级", False, "str", "对酒店星级有要求吗？"),
        "room_type": SlotSpec("room_type", "房型", False, "str", "需要大床房还是双床房？"),
        "guests": SlotSpec("guests", "入住人数", False, "int", "几位入住？"),
        "budget": SlotSpec("budget", "预算/晚", False, "float", "每晚预算大概多少？"),
    }
    urgency_days = 1
    min_saving_margin = 0.05

    def normalize_slots(self, raw: dict[str, Any]) -> dict[str, Any]:
        from .flight import FlightCategory

        slots = super().normalize_slots(raw)
        if slots.get("city"):
            slots["city"] = FlightCategory.canonical_city(slots["city"]) or slots["city"]
        if not slots.get("checkout_date") and slots.get("checkin_date"):
            try:
                checkin = date.fromisoformat(str(slots["checkin_date"]))
                slots["checkout_date"] = (checkin + timedelta(days=1)).isoformat()
            except ValueError:
                pass
        return slots

    def validate_slots(self, slots: dict[str, Any]) -> list[Issue]:
        issues = super().validate_slots(slots)
        checkin = parse_iso(str(slots.get("checkin_date") or ""))
        checkout = parse_iso(str(slots.get("checkout_date") or ""))
        if checkin and checkout and checkout <= checkin:
            issues.append(
                Issue("fatal", "INVALID_STAY", "离店日期必须晚于入住日期", "checkout_date")
            )
        return issues

    def nights(self, intent: PurchaseIntent) -> int:
        checkin = parse_iso(str(intent.slots.get("checkin_date") or ""))
        checkout = parse_iso(str(intent.slots.get("checkout_date") or ""))
        if not checkin or not checkout:
            return 1
        return max(1, (checkout.date() - checkin.date()).days)

    def title(self, intent: PurchaseIntent) -> str:
        return (
            f"{intent.slots.get('city', '')} 酒店 "
            f"{intent.slots.get('checkin_date', '')} 至 {intent.slots.get('checkout_date', '')}"
        ).strip()

    def build_query(self, intent: PurchaseIntent) -> CategoryQuery:
        params = {
            "city": intent.slots.get("city"),
            "checkin_date": intent.slots.get("checkin_date"),
            "checkout_date": intent.slots.get("checkout_date"),
            "star_level": intent.slots.get("star_level"),
            "room_type": intent.slots.get("room_type"),
            "guests": intent.slots.get("guests") or 1,
            "nights": self.nights(intent),
        }
        return CategoryQuery(self.key, params)

    def filter_coupon(self, coupon: Coupon, intent: PurchaseIntent) -> bool:
        if not super().filter_coupon(coupon, intent):
            return False
        cities = coupon.scope.get("cities") or []
        if cities and intent.slots.get("city") not in cities:
            return False
        return True

    def rank_key(self, offer: Any, preferences: dict[str, Any] | None = None) -> tuple:
        return super().rank_key(offer, preferences)

    def build_deep_link(self, offer: Any) -> str:
        attrs = getattr(offer, "attributes", {}) or {}
        city = attrs.get("city") or ""
        checkin = attrs.get("checkin_date") or ""
        checkout = attrs.get("checkout_date") or ""
        coupon_code = getattr(offer, "coupon_code", "") or ""
        platform = getattr(offer, "platform", "")
        if platform == "ctrip":
            params = urlencode(
                {
                    "city": city,
                    "checkIn": checkin,
                    "checkOut": checkout,
                    "promo": coupon_code,
                }
            )
            return f"https://hotels.ctrip.com/hotels/list?{params}"
        if platform == "fliggy":
            params = urlencode({"city": city, "checkin": checkin, "checkout": checkout, "promo": coupon_code})
            return f"https://www.fliggy.com/hotel/search?{params}"
        params = urlencode({"keyword": f"{city}酒店 {checkin} {coupon_code}"})
        base = {
            "jd": "https://hotel.jd.com/",
            "douyin": "https://www.douyin.com/search/",
        }.get(platform, "https://s.taobao.com/search?")
        return f"{base}?{params}" if "?" not in base else f"{base}{params}"

    def build_prediction_features(
        self,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        features = super().build_prediction_features(intent, snapshots, coupons, at)
        at = at or now()
        checkin = parse_iso(str(intent.slots.get("checkin_date") or ""))
        features.update(
            {
                "city": intent.slots.get("city"),
                "nights": self.nights(intent),
                "days_to_checkin": (checkin.date() - at.date()).days if checkin else None,
                "seasonal_factor": 1.1 if checkin and (checkin.date() - at.date()).days <= 2 else 1.0,
            }
        )
        return features

    def notification_plan(self, booking: Any) -> list[NotificationSpec]:
        slots = booking.slots if hasattr(booking, "slots") else booking.get("slots", {})
        checkin = parse_iso(str(slots.get("checkin_date") or ""))
        if not checkin:
            return []
        checkin_at = checkin.replace(hour=15, minute=0, second=0, microsecond=0)
        plans = [
            NotificationSpec(
                type="checkin_reminder",
                title="入住提醒",
                body=f"今天可入住 {slots.get('city', '')} 酒店，请携带有效证件。",
                at=iso(checkin_at - timedelta(hours=4)),
                lead_hours=4,
            ),
            NotificationSpec(
                type="cancel_deadline",
                title="免费取消截止提醒",
                body="今日为免费取消截止日，如需调整请尽快操作。",
                at=iso(checkin_at - timedelta(hours=24)),
                lead_hours=24,
            ),
        ]
        return plans

    def validate_quote(self, quote: dict[str, Any]) -> list[Issue]:
        issues = super().validate_quote(quote)
        if not quote.get("room_type"):
            issues.append(Issue("warn", "MISSING_ROOM_TYPE", "缺少房型信息", "room_type"))
        return issues

    def is_urgent(
        self,
        intent: PurchaseIntent,
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> bool:
        at = at or now()
        checkin = parse_iso(str(intent.slots.get("checkin_date") or ""))
        if not checkin:
            return False
        days = (checkin.date() - at.date()).days
        soon_coupon = any(
            c.is_valid(at) and c.valid_to and (parse_iso(c.valid_to) or at) <= at + timedelta(hours=48)
            for c in coupons
            if self.filter_coupon(c, intent)
        )
        return 0 <= days <= 1 or soon_coupon
