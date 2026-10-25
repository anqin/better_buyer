"""适配器注册表：(品类, 平台) 路由与降级。"""

from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..core.logging import get_logger
from .base import AdapterHealth, PlatformAdapter, PlatformConfig
from .mock import MockAdapter
from .official_api import AggregatorAdapter, OfficialAPIAdapter
from .playwright_adapter import PlaywrightAdapter

logger = get_logger("better_buyer.adapters")


class AdapterRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = settings.platform_mode
        self.platforms: dict[str, PlatformConfig] = {}
        raw = settings.platforms.get("platforms") or []
        for item in raw:
            config = PlatformConfig.from_dict(item)
            if config.key:
                self.platforms[config.key] = config
        self.category_adapters: dict[str, dict[str, str]] = (
            settings.platforms.get("category_adapters") or {}
        )
        self._cache: dict[tuple[str, str], PlatformAdapter] = {}
        self._fallback_logged: set[str] = set()

    # ------------------------------------------------------------------
    def provider_for(self, category: str, platform: str) -> str:
        override = (self.category_adapters.get(category) or {}).get(platform)
        if override:
            return override
        if self.mode == "mock":
            return "mock"
        config = self.platforms.get(platform)
        return config.provider if config else "playwright"

    def list_platforms(self, category: str) -> list[PlatformConfig]:
        return [
            platform
            for platform in self.platforms.values()
            if platform.supports(category)
        ]

    def get(self, category: str, platform: str) -> PlatformAdapter | None:
        key = (category, platform)
        if key in self._cache:
            return self._cache[key]
        platform_config = self.platforms.get(platform)
        if platform_config and not platform_config.supports(category):
            return None
        provider = self.provider_for(category, platform)

        adapter: PlatformAdapter
        if self.mode == "mock" or provider == "mock":
            adapter = MockAdapter(platform, mode="mock")
        elif provider == "playwright":
            candidate = PlaywrightAdapter(platform, self.settings.platforms.get("playwright"))
            if candidate.health().ok:
                adapter = candidate
            else:
                if platform not in self._fallback_logged:
                    logger.info("%s 平台 Playwright 不可用，降级 Mock", platform)
                    self._fallback_logged.add(platform)
                adapter = MockAdapter(platform, mode="fallback")
        elif provider == "official_api":
            section = (self.settings.platforms.get("official_api") or {}).get(platform) or {}
            adapter = OfficialAPIAdapter(platform, section)
        elif provider == "aggregator":
            adapter = AggregatorAdapter(platform, self.settings.platforms.get("aggregator"))
        else:
            logger.warning("未知 provider=%s，跳过 %s", provider, platform)
            return None

        self._cache[key] = adapter
        return adapter

    def health_all(self) -> list[AdapterHealth]:
        items: list[AdapterHealth] = []
        for platform in self.platforms.values():
            adapter = self.get(platform.categories[0], platform.key) if platform.categories else None
            items.append(
                adapter.health()
                if adapter
                else AdapterHealth(platform.key, False, "none", "无可用品类")
            )
        return items
