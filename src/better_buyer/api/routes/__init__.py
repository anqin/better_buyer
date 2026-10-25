"""API 路由注册。"""

from __future__ import annotations

from fastapi import APIRouter

from . import auth, chat, system, trading

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(trading.router)
api_router.include_router(system.router)

__all__ = ["api_router"]
