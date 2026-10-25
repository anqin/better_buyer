"""信息获取智能体：多平台价格与优惠券采集、降级与落库。"""

from __future__ import annotations

from typing import Any

from ..adapters.registry import AdapterRegistry
from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..core.logging import get_logger
from ..models import CategoryQuery, Coupon, PriceSnapshot, PurchaseIntent
from ..services.memory import MemoryStore
from ..storage.md_export import MarkdownExporter
from ..storage.repositories import RepositoryHub

logger = get_logger("better_buyer.collector")


class CollectionAgent:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        adapters: AdapterRegistry,
        repos: RepositoryHub,
        memory: MemoryStore | None = None,
        exporter: MarkdownExporter | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.adapters = adapters
        self.repos = repos
        self.memory = memory
        self.exporter = exporter

    # ------------------------------------------------------------------
    def platforms_for(self, category: str) -> list[str]:
        return [item.key for item in self.adapters.list_platforms(category)]

    def collect(
        self,
        intent: PurchaseIntent,
        platforms: list[str] | None = None,
    ) -> dict[str, Any]:
        strategy = self.registry.get(intent.category)
        query = strategy.build_query(intent)
        targets = platforms or self.platforms_for(intent.category)
        snapshots: list[PriceSnapshot] = []
        coupons: list[Coupon] = []
        errors: list[dict[str, str]] = []

        for platform in targets:
            adapter = self.adapters.get(intent.category, platform)
            if adapter is None:
                errors.append({"platform": platform, "error": "平台不支持该品类"})
                continue
            try:
                for snapshot in adapter.fetch_prices(query):
                    snapshot.category = intent.category
                    snapshot.platform = snapshot.platform or platform
                    self.repos.prices.upsert(snapshot)
                    snapshots.append(snapshot)
            except NotImplementedError as exc:
                errors.append({"platform": platform, "error": f"适配器未实现：{exc}"})
                logger.warning("%s %s 价格采集未实现：%s", intent.category, platform, exc)
            except Exception as exc:
                errors.append({"platform": platform, "error": str(exc)})
                logger.warning("%s %s 价格采集失败：%s", intent.category, platform, exc)

            try:
                for coupon in adapter.fetch_coupons(intent.category, query):
                    coupon.category = intent.category
                    coupon.platform = coupon.platform or platform
                    self.repos.coupons.upsert(coupon)
                    coupons.append(coupon)
            except NotImplementedError as exc:
                errors.append({"platform": platform, "error": f"券适配器未实现：{exc}"})
            except Exception as exc:
                errors.append({"platform": platform, "error": str(exc)})
                logger.warning("%s %s 券采集失败：%s", intent.category, platform, exc)

        eligible = [
            coupon
            for coupon in coupons
            if strategy.filter_coupon(coupon, intent) and coupon.is_valid()
        ]
        report = {
            "category": intent.category,
            "query": query.to_dict(),
            "platforms": targets,
            "price_count": len(snapshots),
            "coupon_count": len(coupons),
            "eligible_coupons": [coupon.to_dict() for coupon in eligible],
            "errors": errors,
        }
        self.repos.events.log(
            "collector",
            "collect",
            {
                "price_count": len(snapshots),
                "coupon_count": len(coupons),
                "eligible_count": len(eligible),
                "errors": errors,
            },
            category=intent.category,
        )
        if self.exporter is not None:
            try:
                self.exporter.export_all()
            except Exception as exc:  # 导出失败不应影响采集
                logger.warning("Markdown 导出失败：%s", exc)
        return report

    # ------------------------------------------------------------------
    def collect_coupons(self, category: str) -> int:
        """定时任务用：不带具体意图，抓取品类通用券。"""
        if self.registry.maybe_get(category) is None:
            return 0
        query = CategoryQuery(category, {})
        count = 0
        for platform in self.platforms_for(category):
            adapter = self.adapters.get(category, platform)
            if adapter is None:
                continue
            try:
                for coupon in adapter.fetch_coupons(category, query):
                    coupon.category = category
                    coupon.platform = coupon.platform or platform
                    self.repos.coupons.upsert(coupon)
                    count += 1
            except Exception as exc:
                logger.warning("%s %s 通用券采集失败：%s", category, platform, exc)
        self.repos.events.log(
            "collector", "collect_coupons", {"count": count}, category=category
        )
        return count

    def collect_all_coupons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for strategy in self.registry.enabled():
            counts[strategy.key] = self.collect_coupons(strategy.key)
        if self.exporter is not None:
            self.exporter.export_coupons()
        return counts
