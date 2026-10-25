"""设备注册、配对码与令牌刷新。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_container, get_current_user
from ..schemas import PairingVerifyRequest, RefreshRequest, RegisterRequest

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(container, user: dict) -> dict:
    return {
        "user": {"id": user["id"], "display_name": user.get("display_name", "")},
        "access_token": container.tokens.issue(user["id"], "access"),
        "refresh_token": container.tokens.issue(user["id"], "refresh"),
        "token_type": "bearer",
        "expires_in": container.tokens.access_minutes * 60,
    }


@router.post("/register")
def register(body: RegisterRequest, request: Request) -> dict:
    container = get_container(request)
    user = container.repos.users.get_or_create(body.device_id, body.display_name)
    return _tokens(container, user)


@router.post("/pairing/code")
def pairing_code(request: Request, user: dict = Depends(get_current_user)) -> dict:
    container = get_container(request)
    code = container.pairing.generate(user["id"])
    return {
        "code": code,
        "expires_in": container.pairing.ttl_seconds,
        "max_attempts": container.pairing.max_attempts,
        "tip": "在手机端 PWA 输入此配对码完成登录",
    }


@router.post("/pairing/verify")
def pairing_verify(body: PairingVerifyRequest, request: Request) -> dict:
    container = get_container(request)
    bound_user_id = container.pairing.consume(body.code)
    if bound_user_id is None:
        raise HTTPException(status_code=400, detail="配对码错误或已过期")
    if bound_user_id:
        user = container.repos.users.get(bound_user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="配对账号不存在")
    else:
        user = container.repos.users.get_or_create(body.device_id, body.display_name)
    return _tokens(container, user)


@router.post("/refresh")
def refresh(body: RefreshRequest, request: Request) -> dict:
    container = get_container(request)
    payload = container.tokens.verify(body.refresh_token, "refresh")
    if not payload:
        raise HTTPException(status_code=401, detail="刷新令牌无效或已过期")
    user = container.repos.users.get(str(payload.get("sub")))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return _tokens(container, user)


@router.get("/me")
def me(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return {"id": user["id"], "display_name": user.get("display_name", "")}
