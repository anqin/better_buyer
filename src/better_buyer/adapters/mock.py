"""确定性 Mock 适配器：离线演示与测试，不依赖网络与账号。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from ..core.datetime_utils import days_until
from ..models import CategoryQuery, Coupon, Offer, PriceSnapshot, iso, now, parse_iso
from .base import AdapterHealth

PLATFORM_FACTOR = {"ctrip": 1.03, "fliggy": 0.99, "taobao": 1.01, "jd": 0.97, "douyin": 0.96}
COUPON_SCOPES = {
    "flight": "机票",
    "hotel": "酒店",
    "moutai": "白酒",
    "custom_example": "自定义",
}


def _seed(*parts: Any) -> int:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


class MockAdapter:
    """按 (平台, 品类, 查询, 小时) 生成稳定的假数据。"""

    source = "mock"

    def __init__(self, platform: str, mode: str = "mock"):
        self.platform = platform
        self.mode = mode

    # ------------------------------------------------------------------
    def health(self) -> AdapterHealth:
        return AdapterHealth(self.platform, True, self.source, "mock 数据源可用")

    def fetch_prices(self, query: CategoryQuery) -> list[PriceSnapshot]:
        params = query.params
        category = query.category
        hour = now().strftime("%Y-%m-%dT%H")
        seed = _seed(self.platform, category, params, hour)
        captured = iso(now())

        if category == "flight":
            origin = params.get("origin") or "北京"
            destination = params.get("destination") or "上海"
            depart_date = params.get("depart_date") or (now().date() + timedelta(days=7)).isoformat()
            days = days_until(depart_date)
            base = 620 + seed % 780
            if days <= 2:
                base *= 1.28
            elif days <= 7:
                base *= 1.12
            elif days >= 30:
                base *= 0.93
            base *= PLATFORM_FACTOR.get(self.platform, 1.0)
            hour_offset = _seed(self.platform, category, params, "time") % 3
            depart_time = ["08:10", "13:35", "19:20"][hour_offset]
            cabin = params.get("cabin") or "经济舱"
            if cabin == "商务舱":
                base *= 3.2
            elif cabin == "头等舱":
                base *= 5.0
            flight_no = f"{['MU', 'CA', 'CZ', 'HU', 'FM'][seed % 5]}{1000 + seed % 8000}"
            price = round(base, 2)
            return [
                PriceSnapshot(
                    category=category,
                    platform=self.platform,
                    item_id=f"{flight_no}-{depart_time}",
                    item_name=f"{origin}→{destination} {flight_no} {depart_time}",
                    price=price,
                    tax=round(50 + seed % 120, 2),
                    attributes={
                        "origin": origin,
                        "destination": destination,
                        "depart_date": depart_date,
                        "depart_time": depart_time,
                        "cabin": cabin,
                        "flight_no": flight_no,
                        "seats_left": 3 + seed % 20,
                    },
                    source=self.source,
                    confidence=0.82,
                    captured_at=captured,
                )
            ]

        if category == "hotel":
            city = params.get("city") or "上海"
            checkin = params.get("checkin_date") or now().date().isoformat()
            checkout = params.get("checkout_date") or (now().date() + timedelta(days=1)).isoformat()
            nights = max(1, int(params.get("nights") or 1))
            base = 260 + seed % 620
            base *= PLATFORM_FACTOR.get(self.platform, 1.0)
            star = int(params.get("star_level") or (3 + seed % 3))
            room = params.get("room_type") or ("大床房" if seed % 2 else "双床房")
            price = round(base, 2)
            return [
                PriceSnapshot(
                    category=category,
                    platform=self.platform,
                    item_id=f"HTL{seed % 100000}",
                    item_name=f"{city}精选酒店 {star}星 {room}",
                    price=price,
                    tax=0.0,
                    attributes={
                        "city": city,
                        "checkin_date": checkin,
                        "checkout_date": checkout,
                        "nights": nights,
                        "room_type": room,
                        "star_level": star,
                        "in_stock": True,
                    },
                    source=self.source,
                    confidence=0.8,
                    captured_at=captured,
                )
            ]

        if category == "moutai":
            sku = params.get("sku") or "飞天茅台53度500ml"
            quantity = int(params.get("quantity") or 1)
            base = 1500 + seed % 900
            base *= PLATFORM_FACTOR.get(self.platform, 1.0)
            in_stock = seed % 5 != 0
            return [
                PriceSnapshot(
                    category=category,
                    platform=self.platform,
                    item_id=f"MT{seed % 100000}",
                    item_name=sku,
                    price=round(base, 2),
                    tax=0.0,
                    attributes={
                        "sku": sku,
                        "spec": params.get("spec") or "500ml 单瓶",
                        "quantity": quantity,
                        "in_stock": in_stock,
                    },
                    source=self.source,
                    confidence=0.78,
                    captured_at=captured,
                )
            ]

        item_id = str(params.get("item_id") or f"ITEM{seed % 10000}")
        return [
            PriceSnapshot(
                category=category,
                platform=self.platform,
                item_id=item_id,
                item_name=item_id,
                price=round(80 + seed % 400, 2),
                attributes={
                    "item_id": item_id,
                    "quantity": int(params.get("quantity") or 1),
                    "in_stock": True,
                },
                source=self.source,
                confidence=0.75,
                captured_at=captured,
            )
        ]

    # ------------------------------------------------------------------
    def fetch_coupons(self, category: str, query: CategoryQuery | None) -> list[Coupon]:
        scope_name = COUPON_SCOPES.get(category, "自定义")
        day = now().strftime("%Y%m%d")
        seed = _seed(self.platform, category, day, "coupon")
        collected = iso(now())
        valid_from = iso(now() - timedelta(days=1))
        coupons: list[Coupon] = []

        amount = 30 + seed % 120
        scope: dict[str, Any] = {"categories": [scope_name]}
        if query:
            params = query.params
            if category == "flight":
                scope["routes"] = [params.get("route")]
                scope["cabins"] = [params.get("cabin") or "经济舱"]
            elif category == "hotel":
                scope["cities"] = [params.get("city")]
            elif category == "moutai":
                scope["skus"] = [str(params.get("sku") or "茅台")[:6]]
        coupons.append(
            Coupon(
                category=category,
                platform=self.platform,
                coupon_code=f"{self.platform.upper()}-{category.upper()}-{amount}-{seed % 97}",
                title=f"{self.platform} {scope_name}满减券 满500减{amount}",
                coupon_type="amount",
                amount=float(amount),
                min_spend=500.0,
                valid_from=valid_from,
                valid_to=iso(now() + timedelta(days=1 + seed % 20)),
                scope=scope,
                source=self.source,
                confidence=0.85,
                collected_at=collected,
            )
        )
        coupons.append(
            Coupon(
                category=category,
                platform=self.platform,
                coupon_code=f"{self.platform.upper()}-{category.upper()}-PCT-{seed % 13}",
                title=f"{self.platform} {scope_name} 9.5 折券",
                coupon_type="discount",
                discount_rate=0.05,
                min_spend=800.0,
                valid_from=valid_from,
                valid_to=iso(now() + timedelta(days=3 + seed % 10)),
                scope=scope,
                source=self.source,
                confidence=0.8,
                collected_at=collected,
            )
        )
        # 跨品类券：用于验证硬过滤
        other_scope = "酒店" if scope_name != "酒店" else "机票"
        coupons.append(
            Coupon(
                category=category,
                platform=self.platform,
                coupon_code=f"{self.platform.upper()}-{category.upper()}-CROSS-{seed % 31}",
                title=f"{self.platform} {other_scope}专享券（不可用于当前品类）",
                coupon_type="amount",
                amount=200.0,
                min_spend=300.0,
                valid_from=valid_from,
                valid_to=iso(now() + timedelta(days=10)),
                scope={"categories": [other_scope]},
                source=self.source,
                confidence=0.6,
                collected_at=collected,
            )
        )
        return coupons

    def issue_deep_link(self, offer: Offer) -> str:
        return f"https://example.com/{self.platform}?item={offer.item_id}&coupon={offer.coupon_code or ''}"
