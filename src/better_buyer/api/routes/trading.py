"""预约、盯盘、报价、订单与凭据接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...models import PurchaseIntent
from ..deps import get_container, get_current_user, require_booking
from ..schemas import (
    CredentialRequest,
    OffersRequest,
    OrderCreateRequest,
    ReceiptRequest,
    WatchRequest,
)

router = APIRouter(tags=["trading"])


@router.get("/bookings")
def list_bookings(
    request: Request,
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    container = get_container(request)
    bookings = container.repos.bookings.list(user["id"], limit=limit)
    return {"bookings": [booking.to_dict() for booking in bookings]}


@router.get("/bookings/{booking_id}")
def booking_detail(
    booking_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    booking = require_booking(container, booking_id, user["id"])
    decision = container.repos.decisions.latest(booking.id)
    return {
        "booking": booking.to_dict(),
        "decision": decision,
        "offers": [offer.to_dict() for offer in container.repos.offers.list(booking.id, booking.category)],
        "watch": container.repos.watches.get_active(booking.id),
        "notifications": container.repos.notifications.list(booking.id),
    }


@router.post("/bookings/{booking_id}/watch")
def start_watch(
    booking_id: str,
    body: WatchRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    booking = require_booking(container, booking_id, user["id"])
    task_id = container.watcher.start(booking)
    if body.interval_minutes:
        container.repos.watches.update(task_id, interval_minutes=max(1, body.interval_minutes))
    return {"task_id": task_id, "status": "running"}


@router.delete("/bookings/{booking_id}/watch")
def stop_watch(
    booking_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    require_booking(container, booking_id, user["id"])
    container.watcher.stop(booking_id)
    return {"status": "stopped"}


@router.get("/bookings/{booking_id}/offers")
def list_offers(
    booking_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    booking = require_booking(container, booking_id, user["id"])
    offers = container.repos.offers.list(booking.id, booking.category)
    return {"offers": [offer.to_dict() for offer in offers]}


@router.post("/bookings/{booking_id}/offers")
def prepare_offers(
    booking_id: str,
    body: OffersRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    booking = require_booking(container, booking_id, user["id"])
    intent = PurchaseIntent(
        category=booking.category,
        slots=dict(booking.slots),
        budget=booking.budget,
        preferences=dict(booking.attributes.get("preferences") or {}),
    )
    offers = container.purchase.prepare_offers(booking, intent, refresh=body.refresh)
    return {"offers": [offer.to_dict() for offer in offers]}


@router.post("/bookings/{booking_id}/orders")
def create_order(
    booking_id: str,
    body: OrderCreateRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    booking = require_booking(container, booking_id, user["id"])
    offers = container.repos.offers.list(booking.id, booking.category)
    if not offers:
        raise HTTPException(status_code=409, detail="没有可用报价，请先刷新")
    if body.offer_index >= len(offers):
        raise HTTPException(status_code=400, detail="方案序号超出范围")
    offer = offers[body.offer_index]
    intent = PurchaseIntent(
        category=booking.category,
        slots=dict(booking.slots),
        budget=booking.budget,
        preferences=dict(booking.attributes.get("preferences") or {}),
    )
    order = container.purchase.create_order(booking, offer, intent)
    credential = None
    if body.account:
        credential = container.purchase.save_credential(
            user["id"],
            offer.platform,
            body.account,
            body.secret,
            auth_type="encrypted_session" if body.secret else body.auth_type,
        )
    return {
        "order": order.to_dict(),
        "deep_link": order.deep_link,
        "credential": credential,
        "next": "打开深链完成支付后提交订单号回执",
    }


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    order = container.repos.orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    booking = container.repos.bookings.get(order.booking_id)
    if booking is None or booking.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {"order": order.to_dict()}


@router.post("/orders/{order_id}/receipt")
def submit_receipt(
    order_id: str,
    body: ReceiptRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    order = container.repos.orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    booking = container.repos.bookings.get(order.booking_id)
    if booking is None or booking.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    return container.purchase.register_receipt(order, body.order_no)


@router.get("/credentials")
def list_credentials(
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    return {"credentials": container.purchase.credential_status(user["id"])}


@router.post("/credentials")
def save_credential(
    body: CredentialRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    return container.purchase.save_credential(
        user["id"],
        body.platform,
        body.account,
        body.secret,
        auth_type=body.auth_type,
        expires_at=body.expires_at,
    )
