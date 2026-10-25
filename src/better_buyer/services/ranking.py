"""方案排序与选券：券硬过滤 + 实付价计算。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..categories.registry import CategoryRegistry
from ..models import Coupon, Offer, PriceSnapshot, PurchaseIntent, new_id, now, now_iso


class OfferRanker:
    def __init__(self, registry: CategoryRegistry):
        self.registry = registry

    # ------------------------------------------------------------------
    def best_coupon(
        self,
        strategy: Any,
        intent: PurchaseIntent,
        coupons: list[Coupon],
        platform: str,
        price: float,
        at: datetime | None = None,
    ) -> tuple[Coupon | None, float]:
        at = at or now()
        best: Coupon | None = None
        best_discount = 0.0
        for coupon in coupons:
            if coupon.platform != platform:
                continue
            if not strategy.filter_coupon(coupon, intent):
                continue
            if not coupon.is_valid(at):
                continue
            discount = coupon.discount_for(price)
            if discount <= 0:
                continue
            if discount > best_discount or (
                discount == best_discount
                and best is not None
                and coupon.valid_to
                and coupon.valid_to < best.valid_to
            ):
                best = coupon
                best_discount = discount
        return best, round(best_discount, 2)

    def build_offer(
        self,
        strategy: Any,
        intent: PurchaseIntent,
        snapshot: PriceSnapshot,
        coupons: list[Coupon],
        booking_id: str,
        at: datetime | None = None,
    ) -> Offer | None:
        if snapshot.category != strategy.key or snapshot.quarantined:
            return None
        price = round(snapshot.total_price, 2)
        if price <= 0:
            return None
        coupon, discount = self.best_coupon(strategy, intent, coupons, snapshot.platform, price, at)
        paid = round(max(0.0, price - discount), 2)
        quantity = int(intent.slots.get("quantity") or 1)
        total_paid = round(paid * quantity, 2) if strategy.key == "moutai" else paid
        attributes = dict(snapshot.attributes)
        attributes.update(
            {
                **intent.slots,
                "item_name": snapshot.item_name,
                "source": snapshot.source,
                "total_paid": total_paid,
                "quantity": quantity,
            }
        )
        in_stock = bool(snapshot.attributes.get("in_stock", True))
        return Offer(
            id=new_id("o"),
            booking_id=booking_id,
            category=strategy.key,
            platform=snapshot.platform,
            item_id=snapshot.item_id,
            item_name=snapshot.item_name,
            price=price,
            coupon_id=str(coupon.id) if coupon and coupon.id is not None else (
                coupon.coupon_code if coupon else None
            ),
            coupon_code=coupon.coupon_code if coupon else None,
            discount=discount,
            paid_price=paid,
            attributes=attributes,
            quoted_at=now_iso(),
            coupon_title=coupon.title if coupon else "",
            in_stock=in_stock,
            confidence=snapshot.confidence,
        )

    def rank(
        self,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list[Coupon],
        booking_id: str,
        at: datetime | None = None,
    ) -> list[Offer]:
        strategy = self.registry.get(intent.category)
        offers: list[Offer] = []
        for snapshot in snapshots:
            offer = self.build_offer(strategy, intent, snapshot, coupons, booking_id, at)
            if offer is None:
                continue
            issues = strategy.validate_quote(offer.to_dict() | {"room_type": offer.attributes.get("room_type")})
            if any(issue.level == "fatal" for issue in issues):
                continue
            if strategy.require_in_stock and not offer.in_stock:
                continue
            offers.append(offer)
        return sorted(offers, key=lambda o: strategy.rank_key(o, intent.preferences))

    @staticmethod
    def best_effective(offers: list[Offer]) -> float:
        if not offers:
            return 0.0
        return min(offer.paid_price for offer in offers)

    @staticmethod
    def budget_amount(strategy_key: str, offer: Offer) -> float:
        if strategy_key == "moutai":
            return float(offer.attributes.get("total_paid") or offer.paid_price)
        return float(offer.paid_price)
