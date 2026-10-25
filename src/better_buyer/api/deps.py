"""FastAPI 依赖：容器、鉴权与资源归属校验。"""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, Request

from ..core.container import AppContainer


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return container


def _user_from_token(container: AppContainer, token: str) -> dict[str, Any]:
    payload = container.tokens.verify(token, "access")
    if not payload:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期")
    user = container.repos.users.get(str(payload.get("sub")))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    return _user_from_token(get_container(request), authorization.split(" ", 1)[1].strip())


def user_from_query_token(request: Request, token: str) -> dict[str, Any]:
    return _user_from_token(get_container(request), token)


def require_booking(
    container: AppContainer, booking_id: str, user_id: str
):
    booking = container.repos.bookings.get(booking_id)
    if booking is None or booking.user_id != user_id:
        raise HTTPException(status_code=404, detail="预约不存在")
    return booking
