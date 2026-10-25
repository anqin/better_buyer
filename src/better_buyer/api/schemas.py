"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    device_id: str = Field(min_length=1)
    display_name: str = "用户"


class PairingVerifyRequest(BaseModel):
    code: str = Field(min_length=4, max_length=8)
    device_id: str = Field(min_length=1)
    display_name: str = "手机用户"


class RefreshRequest(BaseModel):
    refresh_token: str


class ChatMessageRequest(BaseModel):
    text: str


class WatchRequest(BaseModel):
    interval_minutes: int | None = None


class OffersRequest(BaseModel):
    refresh: bool = True


class OrderCreateRequest(BaseModel):
    offer_index: int = Field(ge=0)
    account: str = ""
    secret: str = ""
    auth_type: str = "alias_only"


class ReceiptRequest(BaseModel):
    order_no: str = ""


class CredentialRequest(BaseModel):
    platform: str
    account: str
    secret: str = ""
    auth_type: str = "alias_only"
    expires_at: str = ""


class CollectRequest(BaseModel):
    category: str = "flight"
    slots: dict[str, Any] = Field(default_factory=dict)
    budget: float | None = None
