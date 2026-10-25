"""Playwright 采集骨架：低频、公开页面、不绕过验证码。"""

from __future__ import annotations

import random
import time
from typing import Any

from ..core.logging import get_logger
from ..models import CategoryQuery, Coupon, Offer, PriceSnapshot
from .base import AdapterHealth

logger = get_logger("better_buyer.adapters.playwright")

PLATFORM_URLS = {
    "ctrip": "https://flights.ctrip.com/",
    "fliggy": "https://www.fliggy.com/",
    "taobao": "https://www.taobao.com/",
    "jd": "https://www.jd.com/",
    "douyin": "https://www.douyin.com/",
}


class AdapterBlocked(RuntimeError):
    """验证码、登录墙或风控页：不绕过，直接降级。"""


class PlaywrightAdapter:
    """真实采集需要安装 playwright 与 Chromium；缺省失败后由注册表降级 Mock。"""

    source = "playwright"

    def __init__(self, platform: str, config: dict[str, Any] | None = None):
        self.platform = platform
        self.config = dict(config or {})
        self._browser = None
        self._available: bool | None = None
        self._blocked_reason = ""

    def _ensure_available(self) -> None:
        if self._available is not None:
            return
        try:
            import playwright  # noqa: F401

            self._available = True
        except ImportError:
            self._available = False
            self._blocked_reason = "playwright 未安装，已降级 Mock"

    def health(self) -> AdapterHealth:
        self._ensure_available()
        if not self._available:
            return AdapterHealth(self.platform, False, self.source, self._blocked_reason)
        return AdapterHealth(self.platform, True, self.source, "playwright 可用（公开页面低频采集）")

    def fetch_prices(self, query: CategoryQuery) -> list[PriceSnapshot]:
        self._ensure_available()
        if not self._available:
            raise AdapterBlocked(self._blocked_reason)
        # 真实实现：按平台打开搜索结果页，解析公开可见的航班/酒店/商品卡片。
        # 解析器必须配合固定 HTML 快照测试；页面结构变化时抛出 ParserBrokenError。
        raise NotImplementedError(
            f"{self.platform} 的公开页面解析器尚未配置；请补充解析器或使用 mock 模式"
        )

    def fetch_coupons(self, category: str, query: CategoryQuery | None) -> list[Coupon]:
        self._ensure_available()
        if not self._available:
            raise AdapterBlocked(self._blocked_reason)
        raise NotImplementedError(f"{self.platform} 券采集解析器尚未配置")

    def issue_deep_link(self, offer: Offer) -> str:
        base = PLATFORM_URLS.get(self.platform, "https://example.com")
        return f"{base}?item={offer.item_id}"

    # ------------------------------------------------------------------
    def jitter(self) -> None:
        low, high = self.config.get("request_jitter_seconds") or [2, 8]
        time.sleep(random.uniform(float(low), float(high)))

    def detect_block(self, html: str) -> None:
        markers = ("验证码", "captcha", "登录后查看", "安全验证", "访问异常")
        if any(marker in html for marker in markers):
            self._blocked_reason = "页面出现验证码或登录墙，按合规要求跳过"
            raise AdapterBlocked(self._blocked_reason)
