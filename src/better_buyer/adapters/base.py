"""适配器层：平台取数协议、注册与降级。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import CategoryQuery, Coupon, Offer, PriceSnapshot


@dataclass
class AdapterHealth:
    platform: str
    ok: bool
    source: str
    message: str = ""
    blocked_reason: str = ""
    latency_ms: int = 0


class PlatformAdapter(Protocol):
    platform: str

    def fetch_prices(self, query: CategoryQuery) -> list[PriceSnapshot]: ...

    def fetch_coupons(self, category: str, query: CategoryQuery | None) -> list[Coupon]: ...

    def issue_deep_link(self, offer: Offer) -> str: ...

    def health(self) -> AdapterHealth: ...


@dataclass
class PlatformConfig:
    key: str
    name: str
    enabled: bool = True
    provider: str = "playwright"
    categories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlatformConfig":
        return cls(
            key=str(data.get("key", "")),
            name=str(data.get("name", data.get("key", ""))),
            enabled=bool(data.get("enabled", True)),
            provider=str(data.get("provider", "playwright")),
            categories=list(data.get("categories") or []),
        )

    def supports(self, category: str) -> bool:
        return self.enabled and category in self.categories
