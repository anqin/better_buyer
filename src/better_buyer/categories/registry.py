"""品类注册表：加载配置、动态实例化策略、启停与同步。"""

from __future__ import annotations

import copy
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.config import Settings
from ..core.logging import get_logger
from .base import CategoryStrategy

logger = get_logger("better_buyer.categories")


class CategoryConfigError(ValueError):
    pass


@dataclass
class CategoryConfig:
    key: str
    name: str
    strategy: str
    enabled: bool = True
    coupon_scope: str = ""
    slots: dict[str, Any] = field(default_factory=dict)
    ranking: dict[str, Any] = field(default_factory=dict)
    prediction: dict[str, Any] = field(default_factory=dict)
    buy_score_threshold: float = 0.60
    models: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CategoryConfig":
        key = data.get("key")
        strategy = data.get("strategy")
        if not key or not strategy:
            raise CategoryConfigError(f"品类配置缺少 key 或 strategy：{data}")
        return cls(
            key=str(key),
            name=str(data.get("name", key)),
            strategy=str(strategy),
            enabled=bool(data.get("enabled", True)),
            coupon_scope=str(data.get("coupon_scope", "")),
            slots=dict(data.get("slots") or {}),
            ranking=dict(data.get("ranking") or {}),
            prediction=dict(data.get("prediction") or {}),
            buy_score_threshold=float(data.get("buy_score_threshold", 0.60)),
            models=dict(data.get("models") or {}),
            raw=dict(data),
        )

    def merged(self, override: dict[str, Any]) -> "CategoryConfig":
        data = copy.deepcopy(self.raw)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        data.setdefault("key", self.key)
        return CategoryConfig.from_dict(data)

    def to_sync_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "strategy": self.strategy,
            "enabled": self.enabled,
            "coupon_scope": self.coupon_scope,
            "slots": self.slots,
            "ranking": self.ranking,
            "prediction": self.prediction,
            "buy_score_threshold": self.buy_score_threshold,
            "models": self.models,
        }


class CategoryRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.configs: dict[str, CategoryConfig] = {}
        self.strategies: dict[str, CategoryStrategy] = {}
        self.errors: dict[str, str] = {}
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        raw_list = list(self.settings.app.get("categories") or [])
        by_key: dict[str, CategoryConfig] = {}
        for item in raw_list:
            try:
                config = CategoryConfig.from_dict(item)
            except CategoryConfigError as exc:
                logger.warning("跳过非法品类配置：%s", exc)
                continue
            by_key[config.key] = config

        directory = Path(self.settings.categories_dir)
        if directory.exists():
            for path in sorted(directory.glob("*.yaml")):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError) as exc:
                    logger.warning("读取品类配置失败 %s：%s", path, exc)
                    continue
                if not isinstance(data, dict) or not data.get("key"):
                    continue
                key = str(data["key"])
                if key in by_key:
                    try:
                        by_key[key] = by_key[key].merged(data)
                    except CategoryConfigError as exc:
                        self.errors[key] = str(exc)
                else:
                    try:
                        by_key[key] = CategoryConfig.from_dict(data)
                    except CategoryConfigError as exc:
                        self.errors[key] = str(exc)

        for key, config in by_key.items():
            try:
                strategy = self._instantiate(config)
            except Exception as exc:  # 单个品类失败不影响其他品类
                self.errors[key] = str(exc)
                logger.warning("加载品类 %s 失败：%s", key, exc)
                continue
            self.configs[key] = config
            self.strategies[key] = strategy

        logger.info(
            "品类加载完成：成功 %s，失败 %s",
            sorted(self.strategies),
            sorted(self.errors),
        )

    @staticmethod
    def _instantiate(config: CategoryConfig) -> CategoryStrategy:
        module_name, _, class_name = config.strategy.partition(":")
        if not module_name or not class_name:
            raise CategoryConfigError(f"strategy 必须形如 module:Class：{config.strategy}")
        module = importlib.import_module(module_name)
        klass = getattr(module, class_name, None)
        if klass is None:
            raise CategoryConfigError(f"未找到策略类：{config.strategy}")
        if not issubclass(klass, CategoryStrategy):
            raise CategoryConfigError(f"策略类未继承 CategoryStrategy：{config.strategy}")
        instance = klass(config.to_sync_dict())
        if not instance.slot_specs:
            raise CategoryConfigError(f"品类 {config.key} 未定义槽位")
        return instance

    # ------------------------------------------------------------------
    def get(self, key: str) -> CategoryStrategy:
        if key not in self.strategies:
            raise KeyError(f"品类未启用或不存在：{key}")
        return self.strategies[key]

    def maybe_get(self, key: str) -> CategoryStrategy | None:
        return self.strategies.get(key)

    def enabled(self) -> list[CategoryStrategy]:
        return [
            self.strategies[key]
            for key, config in self.configs.items()
            if config.enabled and key in self.strategies
        ]

    def enabled_configs(self) -> list[CategoryConfig]:
        return [config for config in self.configs.values() if config.enabled]

    def schema(self, key: str) -> dict[str, Any]:
        return self.get(key).slot_schema()

    def summary(self) -> list[dict[str, Any]]:
        return [strategy.describe() for strategy in self.enabled()]

    def sync(self, repo: Any) -> None:
        rows = [config.to_sync_dict() for config in self.configs.values()]
        repo.sync(rows)
