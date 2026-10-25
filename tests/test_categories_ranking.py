"""品类策略与方案排序。"""

from __future__ import annotations

from datetime import timedelta

from better_buyer.models import Coupon, PriceSnapshot, PurchaseIntent, now


def _flight_intent() -> PurchaseIntent:
    return PurchaseIntent(
        category="flight",
        slots={
            "origin": "北京",
            "destination": "上海",
            "depart_date": (now().date() + timedelta(days=7)).isoformat(),
            "cabin": "经济舱",
        },
        budget=2000.0,
    )


def test_flight_normalize_and_validate(container):
    strategy = container.registry.get("flight")
    slots = strategy.normalize_slots({"origin": "北京", "destination": "上海", "depart_date": "2026-11-08"})
    assert slots["origin"] == "北京"
    assert slots["destination"] == "上海"
    assert strategy.validate_slots(slots) == []

    issues = strategy.validate_slots({"origin": "北京", "destination": "北京", "depart_date": "2026-11-08"})
    assert any(issue.code == "SAME_CITY" for issue in issues)

    unknown = strategy.validate_slots({"origin": "不存在的城市", "destination": "上海", "depart_date": "2026-11-08"})
    assert any(issue.code == "UNKNOWN_CITY" and issue.level == "fatal" for issue in unknown)


def test_flight_coupon_scope_and_expiry(container):
    strategy = container.registry.get("flight")
    intent = _flight_intent()

    future = (now() + timedelta(days=3)).isoformat(timespec="seconds")
    matched = Coupon(
        category="flight",
        platform="ctrip",
        coupon_code="OK",
        title="匹配航线",
        amount=100,
        min_spend=500,
        scope={"categories": ["flight"], "routes": ["北京-上海"]},
        valid_to=future,
    )
    wrong_route = Coupon(
        category="flight",
        platform="ctrip",
        coupon_code="WRONG",
        title="其他航线",
        amount=100,
        min_spend=500,
        scope={"categories": ["flight"], "routes": ["广州-深圳"]},
        valid_to=future,
    )
    expired = Coupon(
        category="flight",
        platform="ctrip",
        coupon_code="EXPIRED",
        title="过期券",
        amount=100,
        min_spend=500,
        valid_to=(now() - timedelta(days=1)).isoformat(timespec="seconds"),
    )

    assert strategy.filter_coupon(matched, intent) is True
    assert strategy.filter_coupon(wrong_route, intent) is False
    assert expired.is_valid() is False


def test_rank_orders_by_effective_price(container):
    intent = _flight_intent()
    strategy = container.registry.get("flight")
    ranker = container.ranker
    booking_id = "b_test"

    snapshots = [
        PriceSnapshot(category="flight", platform="ctrip", item_id="A", item_name="A", price=800),
        PriceSnapshot(category="flight", platform="jd", item_id="B", item_name="B", price=900),
        PriceSnapshot(category="flight", platform="taobao", item_id="C", item_name="C", price=850, quarantined=True),
    ]
    future = (now() + timedelta(days=3)).isoformat(timespec="seconds")
    coupons = [
        Coupon(
            category="flight", platform="ctrip", coupon_code="C100", title="携程满减",
            amount=100, min_spend=500, scope={"categories": ["flight"]}, valid_to=future,
        ),
        Coupon(
            category="flight", platform="jd", coupon_code="J300", title="京东大额券",
            amount=300, min_spend=500, scope={"categories": ["flight"]}, valid_to=future,
        ),
    ]

    offers = ranker.rank(intent, snapshots, coupons, booking_id)

    assert [offer.platform for offer in offers] == ["jd", "ctrip"]
    assert offers[0].paid_price == 600.0
    assert offers[1].paid_price == 700.0
    assert ranker.best_effective(offers) == 600.0
    assert all(offer.category == strategy.key for offer in offers)
