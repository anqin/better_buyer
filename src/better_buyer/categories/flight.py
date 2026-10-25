"""机票品类策略。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from ..core.datetime_utils import days_until
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

CITY_ALIASES: dict[str, str] = {
    "北京": "北京", "北京市": "北京", "beijing": "北京", "pek": "北京", "首都机场": "北京",
    "上海": "上海", "上海市": "上海", "shanghai": "上海", "sha": "上海", "pvg": "上海",
    "虹桥": "上海", "浦东": "上海",
    "广州": "广州", "广州市": "广州", "guangzhou": "广州", "can": "广州",
    "深圳": "深圳", "深圳市": "深圳", "shenzhen": "深圳", "szx": "深圳",
    "成都": "成都", "成都市": "成都", "chengdu": "成都", "ctu": "成都", "tfu": "成都",
    "杭州": "杭州", "杭州市": "杭州", "hangzhou": "杭州", "hgh": "杭州",
    "西安": "西安", "西安市": "西安", "xian": "西安", "xiy": "西安",
    "重庆": "重庆", "重庆市": "重庆", "chongqing": "重庆", "ckg": "重庆",
    "南京": "南京", "南京市": "南京", "nanjing": "南京", "nkg": "南京",
    "武汉": "武汉", "武汉市": "武汉", "wuhan": "武汉", "wuh": "武汉",
    "长沙": "长沙", "长沙市": "长沙", "changsha": "长沙", "csx": "长沙",
    "厦门": "厦门", "厦门市": "厦门", "xiamen": "厦门", "xmn": "厦门",
    "青岛": "青岛", "青岛市": "青岛", "qingdao": "青岛", "tao": "青岛",
    "天津": "天津", "天津市": "天津", "tianjin": "天津", "tsn": "天津",
    "昆明": "昆明", "昆明市": "昆明", "kunming": "昆明", "kmg": "昆明",
    "三亚": "三亚", "三亚市": "三亚", "sanya": "三亚", "syx": "三亚",
    "海口": "海口", "海口市": "海口", "haikou": "海口", "hak": "海口",
    "香港": "香港", "香港市": "香港", "hongkong": "香港", "hkg": "香港",
}

CITY_CODES: dict[str, str] = {
    "北京": "BJS", "上海": "SHA", "广州": "CAN", "深圳": "SZX", "成都": "CTU",
    "杭州": "HGH", "西安": "XIY", "重庆": "CKG", "南京": "NKG", "武汉": "WUH",
    "长沙": "CSX", "厦门": "XMN", "青岛": "TAO", "天津": "TSN", "昆明": "KMG",
    "三亚": "SYX", "海口": "HAK", "香港": "HKG",
}

FLIGHT_TIME_HOURS = {"上午": 9, "下午": 14, "晚上": 20}


class FlightCategory(CategoryStrategy):
    key = "flight"
    display_name = "机票"
    coupon_scope = "机票"
    slot_specs = {
        "origin": SlotSpec("origin", "出发地", True, "str", "请问从哪个城市出发？"),
        "destination": SlotSpec("destination", "目的地", True, "str", "请问要去哪个城市？"),
        "depart_date": SlotSpec("depart_date", "出发日期", True, "date", "请问哪天出发？例如：下月8号"),
        "depart_time_pref": SlotSpec("depart_time_pref", "出发时段", False, "str", "希望上午、下午还是晚上出发？"),
        "cabin": SlotSpec("cabin", "舱位", False, "str", "需要经济舱、商务舱还是头等舱？"),
        "budget": SlotSpec("budget", "预算", False, "float", "预算大概多少？"),
    }
    urgency_days = 3
    min_saving_margin = 0.03

    # ------------------------------------------------------------------
    @staticmethod
    def canonical_city(value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip()
        if not token:
            return None
        lowered = token.lower()
        if token in CITY_ALIASES:
            return CITY_ALIASES[token]
        if lowered in CITY_ALIASES:
            return CITY_ALIASES[lowered]
        return None

    @staticmethod
    def find_cities(text: str) -> list[str]:
        found: list[str] = []
        lowered = (text or "").lower()
        for alias in sorted(CITY_ALIASES, key=len, reverse=True):
            key = alias.lower()
            if key in lowered:
                city = CITY_ALIASES[alias]
                if city not in found:
                    found.append(city)
        return found

    def normalize_slots(self, raw: dict[str, Any]) -> dict[str, Any]:
        slots = super().normalize_slots(raw)
        for key in ("origin", "destination"):
            if slots.get(key):
                slots[key] = self.canonical_city(slots[key]) or slots[key]
        return slots

    def validate_slots(self, slots: dict[str, Any]) -> list[Issue]:
        issues = super().validate_slots(slots)
        for key, label in (("origin", "出发地"), ("destination", "目的地")):
            if slots.get(key) and not self.canonical_city(slots[key]):
                issues.append(Issue("fatal", "UNKNOWN_CITY", f"无法识别{label}：{slots[key]}", key))
        if slots.get("origin") and slots.get("origin") == slots.get("destination"):
            issues.append(Issue("fatal", "SAME_CITY", "出发地与目的地不能相同", "destination"))
        budget = slots.get("budget")
        if budget is not None:
            try:
                if float(budget) <= 0:
                    issues.append(Issue("fatal", "INVALID_BUDGET", "预算必须大于 0", "budget"))
            except (TypeError, ValueError):
                issues.append(Issue("fatal", "INVALID_BUDGET", "预算格式非法", "budget"))
        return issues

    def title(self, intent: PurchaseIntent) -> str:
        return (
            f"{intent.slots.get('origin', '')} → {intent.slots.get('destination', '')} "
            f"{intent.slots.get('depart_date', '')}"
        ).strip()

    def build_query(self, intent: PurchaseIntent) -> CategoryQuery:
        slots = intent.slots
        params = {
            "origin": self.canonical_city(slots.get("origin")) or slots.get("origin"),
            "destination": self.canonical_city(slots.get("destination")) or slots.get("destination"),
            "depart_date": slots.get("depart_date"),
            "cabin": slots.get("cabin") or "经济舱",
            "depart_time_pref": slots.get("depart_time_pref"),
            "route": f"{self.canonical_city(slots.get('origin')) or slots.get('origin')}-"
                     f"{self.canonical_city(slots.get('destination')) or slots.get('destination')}",
        }
        return CategoryQuery(self.key, params)

    def filter_coupon(self, coupon: Coupon, intent: PurchaseIntent) -> bool:
        if not super().filter_coupon(coupon, intent):
            return False
        query = self.build_query(intent)
        routes = coupon.scope.get("routes") or []
        if routes and query.params["route"] not in routes:
            return False
        cabins = coupon.scope.get("cabins") or []
        cabin = query.params.get("cabin")
        if cabins and cabin and cabin not in cabins:
            return False
        return True

    def build_deep_link(self, offer: Any) -> str:
        attrs = getattr(offer, "attributes", {}) or {}
        origin = self.canonical_city(attrs.get("origin")) or attrs.get("origin", "")
        destination = self.canonical_city(attrs.get("destination")) or attrs.get("destination", "")
        city_from = attrs.get("origin_code") or CITY_CODES.get(origin, origin)
        city_to = attrs.get("destination_code") or CITY_CODES.get(destination, destination)
        depart_date = attrs.get("depart_date", "")
        cabin = attrs.get("cabin", "经济舱")
        coupon_code = getattr(offer, "coupon_code", "") or ""
        platform = getattr(offer, "platform", "")

        if platform == "ctrip":
            params = urlencode(
                {
                    "departure": city_from,
                    "arrival": city_to,
                    "departdate": depart_date,
                    "cabin": cabin,
                    "promo": coupon_code,
                }
            )
            return f"https://flights.ctrip.com/online/list/submit?{params}"
        if platform == "fliggy":
            params = urlencode(
                {"from": city_from, "to": city_to, "date": depart_date, "promo": coupon_code}
            )
            return f"https://www.fliggy.com/flight/search?{params}"
        if platform == "jd":
            params = urlencode({"keyword": f"{origin}{destination}机票{coupon_code}"})
            return f"https://jipiao.jd.com/?{params}"
        if platform == "douyin":
            params = urlencode({"keyword": f"{origin}到{destination}机票 {coupon_code}"})
            return f"https://www.douyin.com/search/?{params}"
        params = urlencode({"keyword": f"{origin}到{destination}机票 {coupon_code}"})
        return f"https://s.taobao.com/search?{params}"

    def build_prediction_features(
        self,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        features = super().build_prediction_features(intent, snapshots, coupons, at)
        at = at or now()
        depart = parse_iso(str(intent.slots.get("depart_date") or ""))
        days = days_until(depart.date(), at.date()) if depart else None
        features.update(
            {
                "route": self.build_query(intent).params["route"],
                "cabin": intent.slots.get("cabin") or "经济舱",
                "depart_date": intent.slots.get("depart_date"),
                "days_to_depart": days,
                "seasonal_factor": self._seasonal_factor(days),
            }
        )
        return features

    @staticmethod
    def _seasonal_factor(days: int | None) -> float:
        if days is None:
            return 1.0
        if days < 0:
            return 1.25
        if days <= 2:
            return 1.20
        if days <= 7:
            return 1.10
        if days <= 14:
            return 1.02
        if days <= 30:
            return 0.97
        return 0.95

    def notification_plan(self, booking: Any) -> list[NotificationSpec]:
        slots = booking.slots if hasattr(booking, "slots") else booking.get("slots", {})
        depart = parse_iso(str(slots.get("depart_date") or ""))
        if not depart:
            return []
        hour = FLIGHT_TIME_HOURS.get(str(slots.get("depart_time_pref") or "上午"), 9)
        flight_at = depart.replace(hour=hour, minute=0, second=0, microsecond=0)
        origin = self.canonical_city(slots.get("origin")) or slots.get("origin") or ""
        destination = self.canonical_city(slots.get("destination")) or slots.get("destination") or ""
        plans: list[NotificationSpec] = []
        for lead, label in ((24, "出发前 24 小时"), (3, "出发前 3 小时")):
            at = flight_at - timedelta(hours=lead)
            plans.append(
                NotificationSpec(
                    type="departure_reminder",
                    title="行程出发提醒",
                    body=f"{label}：{origin} → {destination}，请预留值机与安检时间。",
                    at=iso(at),
                    lead_hours=lead,
                )
            )
        return plans

    def validate_quote(self, quote: dict[str, Any]) -> list[Issue]:
        issues = super().validate_quote(quote)
        if not quote.get("item_id"):
            issues.append(Issue("fatal", "MISSING_FLIGHT_NO", "缺少航班号", "item_id"))
        return issues

    def is_urgent(
        self,
        intent: PurchaseIntent,
        coupons: list[Coupon],
        at: datetime | None = None,
    ) -> bool:
        at = at or now()
        depart = parse_iso(str(intent.slots.get("depart_date") or ""))
        if not depart:
            return False
        days = days_until(depart.date(), at.date())
        soon_coupon = any(
            c.is_valid(at) and c.valid_to and (parse_iso(c.valid_to) or at) <= at + timedelta(hours=48)
            for c in coupons
            if self.filter_coupon(c, intent)
        )
        return 0 <= days <= self.urgency_days or soon_coupon
