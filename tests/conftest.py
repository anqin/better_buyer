"""测试共用夹具：临时项目根 + 复制配置，保证测试不污染真实 data/。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path):
    from better_buyer.core.config import Settings

    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    loaded = Settings.load(
        root=tmp_path,
        config_dir=tmp_path / "config",
        platform_mode="mock",
    )
    loaded.ensure_dirs()
    return loaded


@pytest.fixture()
def container(settings):
    from better_buyer.core.container import AppContainer

    app_container = AppContainer(settings)
    yield app_container
    app_container.close()
