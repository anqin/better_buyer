"""依赖装配：CLI 与 FastAPI 共用同一套对象图。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..adapters.registry import AdapterRegistry
from ..agents.checker import DataChecker
from ..agents.collector import CollectionAgent
from ..agents.interaction import InteractionAgent
from ..agents.main_agent import MainAgent
from ..agents.predictor import PredictorAgent
from ..agents.purchase import PurchaseAgent
from ..agents.watcher import WatchAgent
from ..categories.registry import CategoryRegistry
from ..services.calibration import CalibrationService
from ..services.memory import MemoryStore
from ..services.model_router import ModelRouter
from ..services.notification import NotificationAgent
from ..services.prediction import PricePredictor
from ..services.ranking import OfferRanker
from ..services.scoring import EnsembleScorer
from ..storage.db import Database
from ..storage.md_export import MarkdownExporter
from ..storage.repositories import RepositoryHub
from .config import Settings, load_settings
from .events import EventBus
from .scheduler import Scheduler
from .security import CredentialVault, PairingStore, TokenManager


class AppContainer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.repos = RepositoryHub(self.db)
        self.registry = CategoryRegistry(settings)
        self.registry.sync(self.repos.categories)
        self.adapters = AdapterRegistry(settings)
        self.memory = MemoryStore(settings)
        self.memory.ensure()
        self.exporter = MarkdownExporter(settings, self.repos, self.registry)

        self.router = ModelRouter(settings, self.repos)
        self.scorer = EnsembleScorer(settings.llm_config)
        self.ranker = OfferRanker(self.registry)
        self.predictor = PricePredictor(
            settings, self.registry, self.repos, self.router, self.scorer, self.ranker
        )
        self.notifier = NotificationAgent(settings, self.repos)
        self.collector = CollectionAgent(
            settings, self.registry, self.adapters, self.repos, self.memory, self.exporter
        )
        self.predictor_agent = PredictorAgent(self.repos, self.predictor)
        self.interaction = InteractionAgent(settings, self.registry, self.router)
        self.vault = CredentialVault(settings.master_key)
        self.purchase = PurchaseAgent(
            settings,
            self.registry,
            self.adapters,
            self.repos,
            self.ranker,
            self.collector,
            self.memory,
            self.notifier,
            self.vault,
        )
        self.event_bus = EventBus()
        self.notifier.event_bus = self.event_bus
        self.watcher = WatchAgent(
            settings,
            self.registry,
            self.repos,
            self.collector,
            self.predictor_agent,
            self.notifier,
            self.event_bus,
        )
        self.main = MainAgent(
            settings,
            self.registry,
            self.repos,
            self.interaction,
            self.collector,
            self.predictor_agent,
            self.purchase,
            self.watcher,
            self.memory,
            self.exporter,
        )
        self.checker = DataChecker(settings, self.registry, self.repos)
        self.calibration = CalibrationService(self.repos)
        self.scheduler = Scheduler(
            settings,
            self.repos,
            self.collector,
            self.watcher,
            self.notifier,
            self.checker,
            self.calibration,
            self.event_bus,
        )

        auth = settings.web_config.get("auth") or {}
        self.tokens = TokenManager(
            settings.jwt_secret,
            access_minutes=int(auth.get("access_token_minutes", 15)),
            refresh_days=int(auth.get("refresh_token_days", 30)),
        )
        self.pairing = PairingStore(
            ttl_seconds=int(auth.get("pairing_code_ttl_seconds", 120)),
            max_attempts=int(auth.get("pairing_max_attempts", 5)),
            db=self.db,
        )

    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        root: str | Path | None = None,
        platform_mode: str | None = None,
    ) -> "AppContainer":
        settings = load_settings(root=root, platform_mode=platform_mode)
        return cls(settings)

    def default_user(self) -> dict[str, Any]:
        device_id = os.environ.get("BB_DEVICE_ID", "cli-local")
        return self.repos.users.get_or_create(device_id, "本地用户")

    def start_background(self) -> None:
        self.scheduler.start()
        # 进程重启后，next_run_at 已过期的盯盘任务会在首个 tick 立即执行

    def close(self) -> None:
        self.scheduler.stop()
        self.db.close()
