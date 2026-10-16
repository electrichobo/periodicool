from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from periodicool.config import Settings, load_settings
from periodicool.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(
        {
            "PERIODICOOL_CONFIG_DIR": str(tmp_path / "config"),
            "PERIODICOOL_LIBRARY_DIR": str(tmp_path / "library"),
            "PERIODICOOL_DOWNLOADS_DIR": str(tmp_path / "downloads"),
        }
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c
