"""购买智能体：比价选券、深链生成、凭据保险箱与支付回执。"""

from __future__ import annotations

import json
from typing import Any

from ..adapters.registry import AdapterRegistry
from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..core.logging import get_logger
from ..core.security import CredentialVault, mask_account
from ..models import Booking, Offer, Order, PurchaseIntent, new_id, now_iso, stable_hash
from ..services.memory import MemoryStore
from ..services.notification import NotificationAgent
from ..services.ranking import OfferRanker
from ..storage.repositories import RepositoryHub
from .collector import CollectionAgent

logger = get_logger("better_buyer.purchase")


class PurchaseAgent:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        adapters: AdapterRegistry,
        repos: RepositoryHub,
        ranker: OfferRanker,
        collector: CollectionAgent,
        memory: MemoryStore,
        notifier: NotificationAgent,
        vault: CredentialVault | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.adapters = adapters
        self.repos = repos
        self.ranker = ranker
        self.collector = collector
        self.memory = memory
        self.notifier = notifier
        self.vault = vault or CredentialVault(settings.master_key)

    # ------------------------------------------------------------------
    def prepare_offers(
        self,
        booking: Booking,
        intent: PurchaseIntent | None = None,
        refresh: bool = True,
    ) -> list[Offer]:
        intent = intent or PurchaseIntent(
            category=booking.category,
            slots=dict(booking.slots),
            budget=booking.budget,
            preferences=dict(booking.attributes.get("preferences") or {}),
        )
        if refresh:
            self.collector.collect(intent)
        snapshots = self.repos.prices.latest_by_platform_item(booking.category)
        coupons = self.repos.coupons.active(booking.category)
        offers = self.ranker.rank(intent, snapshots, coupons, booking.id)
        self.repos.offers.replace(booking.id, booking.category, offers)
        self.repos.events.log(
            "purchase",
            "prepare_offers",
            {
                "offer_count": len(offers),
                "best_price": offers[0].paid_price if offers else 0.0,
            },
            booking_id=booking.id,
            category=booking.category,
        )
        return offers

    # ------------------------------------------------------------------
    def build_deep_link(self, offer: Offer) -> str:
        strategy = self.registry.get(offer.category)
        adapter = self.adapters.get(offer.category, offer.platform)
        if adapter is not None and getattr(adapter, "source", "") != "mock":
            try:
                link = adapter.issue_deep_link(offer)
                if link:
                    return link
            except Exception as exc:
                logger.warning("适配器深链失败，回退品类深链：%s", exc)
        return strategy.build_deep_link(offer)

    def create_order(
        self,
        booking: Booking,
        offer: Offer,
        intent: PurchaseIntent | None = None,
    ) -> Order:
        deep_link = self.build_deep_link(offer)
        idem = stable_hash(
            {
                "booking": booking.id,
                "category": offer.category,
                "platform": offer.platform,
                "item": offer.item_id,
                "coupon": offer.coupon_code,
                "paid": offer.paid_price,
            }
        )
        order = Order(
            id=new_id("ord"),
            booking_id=booking.id,
            category=offer.category,
            platform=offer.platform,
            list_price=offer.price,
            coupon_id=offer.coupon_id,
            discount=offer.discount,
            paid_price=offer.paid_price,
            status="draft",
            deep_link=deep_link,
            idempotency_key=idem,
            item_id=offer.item_id,
            item_name=offer.item_name,
            attributes={
                **(intent.slots if intent else booking.slots),
                "coupon_title": offer.coupon_title,
                "quantity": offer.attributes.get("quantity", 1),
                "total_paid": offer.attributes.get("total_paid", offer.paid_price),
            },
        )
        created = self.repos.orders.create(order)
        self.repos.bookings.update(booking.id, status="purchasing")
        self.repos.events.log(
            "purchase",
            "order_draft",
            {"order_id": created.id, "platform": offer.platform, "paid_price": offer.paid_price},
            booking_id=booking.id,
            category=booking.category,
        )
        return created

    # ------------------------------------------------------------------
    def save_credential(
        self,
        user_id: str,
        platform: str,
        account: str,
        secret: str = "",
        auth_type: str = "alias_only",
        expires_at: str = "",
    ) -> dict[str, Any]:
        """账号别名写入 Markdown；敏感原文只在可用时 AES-256-GCM 加密入库。"""
        alias = mask_account(account)
        ciphertext, nonce = "", ""
        if secret:
            if self.vault.available:
                payload = json.dumps({"account": account, "secret": secret}, ensure_ascii=False)
                ciphertext, nonce, _ = self.vault.encrypt(payload)
                auth_type = auth_type if auth_type != "alias_only" else "encrypted_session"
            else:
                logger.warning("未配置 BB_MASTER_KEY，敏感凭据未落库，仅保存脱敏别名")
        self.repos.credentials.upsert(
            user_id=user_id,
            platform=platform,
            account_alias=alias,
            auth_type=auth_type,
            ciphertext=ciphertext,
            nonce=nonce,
            expires_at=expires_at,
        )
        self.memory.write_platform_accounts(self.repos.credentials.list(user_id))
        self.repos.events.log(
            "purchase",
            "credential_saved",
            {"platform": platform, "alias": alias, "encrypted": bool(ciphertext)},
            category="",
        )
        return {
            "platform": platform,
            "account_alias": alias,
            "auth_type": auth_type,
            "encrypted": bool(ciphertext),
        }

    def credential_status(self, user_id: str) -> list[dict[str, Any]]:
        return [
            {
                "platform": row.get("platform"),
                "account_alias": row.get("account_alias"),
                "auth_type": row.get("auth_type"),
                "authorized": bool(row.get("ciphertext")) or row.get("auth_type") == "alias_only",
                "expires_at": row.get("expires_at") or "",
            }
            for row in self.repos.credentials.list(user_id)
        ]

    # ------------------------------------------------------------------
    def register_receipt(self, order: Order, order_no: str = "") -> dict[str, Any]:
        paid_at = now_iso()
        order.status = "paid"
        order.order_no = order_no or order.order_no
        order.paid_at = paid_at
        self.repos.orders.update(
            order.id,
            status="paid",
            order_no=order_no or order.order_no,
            paid_at=paid_at,
        )
        booking = self.repos.bookings.get(order.booking_id, order.category)
        if booking is not None:
            self.repos.bookings.update(booking.id, status="completed")
            self.repos.watches.stop(booking.id)
            self.memory.append_purchase(order, booking)
            strategy = self.registry.get(order.category)
            notification_ids = self.notifier.schedule_order_notifications(order, booking, strategy)
        else:
            notification_ids = []
        self.repos.events.log(
            "purchase",
            "receipt",
            {"order_id": order.id, "paid_price": order.paid_price, "order_no": order_no},
            booking_id=order.booking_id,
            category=order.category,
        )
        return {
            "order_id": order.id,
            "status": "paid",
            "paid_price": order.paid_price,
            "discount": order.discount,
            "platform": order.platform,
            "order_no": order_no or order.order_no,
            "notifications": notification_ids,
        }
