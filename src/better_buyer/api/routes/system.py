"""健康、状态、品类、检查与导出接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...models import PurchaseIntent, now_iso
from ..deps import get_container, get_current_user
from ..schemas import CollectRequest

router = APIRouter(tags=["system"])


@router.get("/health")
def health(request: Request) -> dict:
    container = get_container(request)
    return {
        "ok": True,
        "platform_mode": container.settings.platform_mode,
        "adapters": [item.__dict__ for item in container.adapters.health_all()],
        "models": container.router.health(),
        "vault_available": container.vault.available,
        "scheduler": container.scheduler.status(),
    }


@router.get("/status")
def status(request: Request, user: dict = Depends(get_current_user)) -> dict:
    container = get_container(request)
    return {
        "bookings": len(container.repos.bookings.list(user["id"], limit=200)),
        "running_watches": container.repos.watches.list_running(),
        "pending_notifications": len(container.repos.notifications.due(now_iso())),
        "scheduler": container.scheduler.status(),
        "categories": container.registry.summary(),
    }


@router.get("/categories")
def categories(request: Request) -> dict:
    container = get_container(request)
    return {
        "categories": [
            {
                **strategy.describe(),
                "slots": strategy.slot_schema()["slots"],
            }
            for strategy in container.registry.enabled()
        ]
    }


@router.post("/collect")
def collect(
    body: CollectRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    strategy = container.registry.get(body.category)
    slots = strategy.normalize_slots(body.slots)
    missing = [key for key in strategy.required_slots if not slots.get(key)]
    if missing:
        return {"error": "缺少必填槽位", "missing": missing}
    intent = PurchaseIntent(category=body.category, slots=slots, budget=body.budget)
    return container.collector.collect(intent)


@router.post("/check")
def check(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return get_container(request).checker.run()


@router.post("/export")
def export(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return get_container(request).exporter.export_all()


@router.get("/events/recent")
def recent_events(
    request: Request,
    limit: int = 50,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    return {"events": container.event_bus.recent(limit=max(1, min(limit, 200)))}
