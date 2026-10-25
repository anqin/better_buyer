"""官方开放平台 / 聚合服务适配器预留接入点。"""

from __future__ import annotations

import os
from typing import Any

from ..models import CategoryQuery, Coupon, Offer, PriceSnapshot
from .base import AdapterHealth


class AdapterUnavailable(RuntimeError):
    pass


class OfficialAPIAdapter:
    source = "official_api"

    def __init__(self, platform: str, config: dict[str, Any] | None = None):
        self.platform = platform
        self.config = dict(config or {})

    def _api_key(self) -> str:
        env = self.config.get("api_key_env") or ""
        return os.environ.get(env, "") if env else ""

    def health(self) -> AdapterHealth:
        if not self.config.get("base_url") or not self._api_key():
            return AdapterHealth(
                self.platform, False, self.source, "官方 API 未配置 base_url 或 api_key_env"
            )
        return AdapterHealth(self.platform, True, self.source, "官方 API 已配置")

    def fetch_prices(self, query: CategoryQuery) -> list[PriceSnapshot]:
        raise AdapterUnavailable(f"{self.platform}: 官方 API 适配器待接入")

    def fetch_coupons(self, category: str, query: CategoryQuery | None) -> list[Coupon]:
        raise AdapterUnavailable(f"{self.platform}: 官方 API 适配器待接入")

    def issue_deep_link(self, offer: Offer) -> str:
        return self.config.get("base_url", "") or "https://example.com"


class AggregatorAdapter:
    source = "aggregator"

    def __init__(self, platform: str, config: dict[str, Any] | None = None):
        self.platform = platform
        self.config = dict(config or {})

    def health(self) -> AdapterHealth:
        enabled = bool(self.config.get("enabled"))
        key = os.environ.get(self.config.get("api_key_env", ""), "")
        if not enabled or not self.config.get("base_url") or not key:
            return AdapterHealth(
                self.platform, False, self.source, "三方聚合服务未启用或缺少配置"
            )
        return AdapterHealth(self.platform, True, self.source, "聚合服务已配置")

    def fetch_prices(self, query: CategoryQuery) -> list[PriceSnapshot]:
        raise AdapterUnavailable(f"{self.platform}: 聚合适配器待接入")

    def fetch_coupons(self, category: str, query: CategoryQuery | None) -> list[Coupon]:
        raise AdapterUnavailable(f"{self.platform}: 聚合适配器待接入")

    def issue_deep_link(self, offer: Offer) -> str:
        return self.config.get("base_url", "") or "https://example.com"
