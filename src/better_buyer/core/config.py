"""配置加载：路径、环境变量、app/platforms/llm/web 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误：{path}")
    return data


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def find_root(start: Path | None = None) -> Path:
    """从当前目录向上查找 config/app.yaml，未找到时回退到包所在仓库。"""
    env_root = os.environ.get("BB_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "app.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


@dataclass
class Settings:
    root: Path
    config_dir: Path
    app: dict[str, Any] = field(default_factory=dict)
    platforms: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    web: dict[str, Any] = field(default_factory=dict)
    platform_mode_override: str | None = None

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        root: str | Path | None = None,
        config_dir: str | Path | None = None,
        platform_mode: str | None = None,
    ) -> "Settings":
        base = Path(root).expanduser().resolve() if root else find_root()
        cfg = Path(config_dir).expanduser().resolve() if config_dir else Path(
            os.environ.get("BB_CONFIG_DIR", base / "config")
        )
        settings = cls(
            root=base,
            config_dir=cfg,
            app=_read_yaml(cfg / "app.yaml"),
            platforms=_read_yaml(cfg / "platforms.yaml"),
            llm=_read_yaml(cfg / "llm.yaml"),
            web=_read_yaml(cfg / "web.yaml"),
            platform_mode_override=platform_mode or os.environ.get("BB_PLATFORM_MODE"),
        )
        if not settings.app:
            raise FileNotFoundError(f"未找到主配置：{cfg / 'app.yaml'}")
        return settings

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.memory_dir,
            self.vault_dir,
            self.notifications_dir,
            self.backup_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 常用路径
    # ------------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return _resolve(self.root, os.environ.get("BB_DATA_DIR_NAME", "data"))

    @property
    def memory_dir(self) -> Path:
        return _resolve(self.root, os.environ.get("BB_MEMORY_DIR_NAME", "memory"))

    @property
    def vault_dir(self) -> Path:
        return _resolve(self.root, "vault")

    @property
    def notifications_dir(self) -> Path:
        return _resolve(self.root, "notifications")

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    @property
    def web_dir(self) -> Path:
        return _resolve(self.root, "web")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "better_buyer.db"

    @property
    def categories_dir(self) -> Path:
        raw = os.environ.get(
            "BB_CATEGORY_CONFIG_DIR", self.app.get("categories_dir", "config/categories")
        )
        return _resolve(self.root, raw)

    # ------------------------------------------------------------------
    # 配置项
    # ------------------------------------------------------------------
    @property
    def timezone(self) -> str:
        return self.app.get("timezone", "Asia/Shanghai")

    @property
    def default_category(self) -> str:
        return self.app.get("default_category", "flight")

    @property
    def cities(self) -> list[str]:
        return list(self.app.get("cities") or [])

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.app.get("defaults") or {})

    @property
    def collection(self) -> dict[str, Any]:
        return dict(self.app.get("collection") or {})

    @property
    def check_config(self) -> dict[str, Any]:
        return dict(self.app.get("check") or {})

    @property
    def export_config(self) -> dict[str, Any]:
        return dict(self.app.get("export") or {})

    @property
    def platform_mode(self) -> str:
        return (
            self.platform_mode_override
            or self.platforms.get("mode")
            or "playwright"
        )

    @property
    def watch_interval_minutes(self) -> int:
        return int(
            os.environ.get(
                "BB_WATCH_INTERVAL_MINUTES",
                self.collection.get("price_interval_minutes", 30),
            )
        )

    @property
    def watch_decision_countdown_seconds(self) -> int:
        """向用户发出建议后，未响应自动继续盯盘的倒计时（秒）。"""
        return int(
            os.environ.get(
                "BB_WATCH_DECISION_COUNTDOWN_SECONDS",
                self.collection.get("decision_countdown_seconds", 15),
            )
        )

    @property
    def intent_threshold(self) -> float:
        """实时意图预测进入收集环节的置信度阈值（0~1）。"""
        return float(
            os.environ.get("BB_INTENT_THRESHOLD", self.collection.get("intent_threshold", 0.80))
        )

    @property
    def intent_decay_threshold(self) -> float:
        """已确认意图置信度衰退到该值以下且出现其它品类信号时，才询问是否换个方向。"""
        return float(
            os.environ.get("BB_INTENT_DECAY_THRESHOLD", self.collection.get("intent_decay_threshold", 0.45))
        )

    @property
    def watch_window_days(self) -> int:
        return int(os.environ.get("BB_WATCH_WINDOW_DAYS", "7"))

    @property
    def price_stale_hours(self) -> float:
        return float(self.collection.get("price_stale_hours", 6))

    @property
    def web_config(self) -> dict[str, Any]:
        return dict(self.web)

    @property
    def llm_config(self) -> dict[str, Any]:
        return dict(self.llm)

    # ------------------------------------------------------------------
    # 密钥（只从环境变量读取，不落盘、不打日志）
    # ------------------------------------------------------------------
    @staticmethod
    def env(name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    @property
    def jwt_secret(self) -> str:
        return self.env("BB_JWT_SECRET", "dev-only-change-me")

    @property
    def master_key(self) -> str:
        return self.env("BB_MASTER_KEY", "")

    @property
    def notify_webhook(self) -> str:
        return self.env("BB_NOTIFY_WEBHOOK", "")

    @property
    def public_base_url(self) -> str:
        return self.env("BB_PUBLIC_BASE_URL", self.web.get("public_base_url", ""))


def load_settings(
    root: str | Path | None = None,
    config_dir: str | Path | None = None,
    platform_mode: str | None = None,
) -> Settings:
    settings = Settings.load(root=root, config_dir=config_dir, platform_mode=platform_mode)
    settings.ensure_dirs()
    return settings
