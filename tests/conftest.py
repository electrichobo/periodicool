import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from periodicool.config import Settings, load_settings
from periodicool.main import create_app

ADMIN = ("admin", "correct horse battery")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(
        {
            "PERIODICOOL_CONFIG_DIR": str(tmp_path / "config"),
            "PERIODICOOL_LIBRARY_DIR": str(tmp_path / "library"),
            "PERIODICOOL_DOWNLOADS_DIR": str(tmp_path / "downloads"),
            "PERIODICOOL_JOBS": "off",
        }
    )


def csrf_of(client: TestClient, path: str = "/login") -> str:
    """The CSRF token a page hands this client."""
    html = client.get(path).text
    match = re.search(r'name="csrf-token" content="([^"]*)"', html)
    assert match and match.group(1), f"no CSRF token on {path}"
    return match.group(1)


@pytest.fixture
def anon_client(settings: Settings):
    """A client with no login. The database starts empty, so pages lead to /setup."""
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


@pytest.fixture
def client(settings: Settings):
    """A logged-in client. Its X-CSRF-Token header is set, as htmx does in the browser."""
    with TestClient(create_app(settings)) as c:
        token = csrf_of(c, "/setup")
        c.post(
            "/setup",
            data={
                "csrf_token": token,
                "username": ADMIN[0],
                "password": ADMIN[1],
                "confirm": ADMIN[1],
            },
        )
        c.headers["X-CSRF-Token"] = csrf_of(c, "/ui-kit")
        yield c
