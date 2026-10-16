"""Sanity checks on the release-name fixtures (not parser tests)."""

import json
import re
from pathlib import Path

import pytest

RELEASES = Path(__file__).parent / "fixtures" / "releases"
FIXTURES = sorted(RELEASES.glob("*.json"))
ALLOWED_KEYS = {"title", "size", "publish_date", "categories"}
LEAK = re.compile(r"apikey|api_key|https?://|\b\d{1,3}(\.\d{1,3}){3}\b", re.IGNORECASE)


def test_fixtures_exist():
    assert len(FIXTURES) >= 8


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_shape(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"magazine", "query", "category", "collected", "releases"} <= data.keys()
    assert data["releases"], "no releases"
    titles = [r["title"] for r in data["releases"]]
    assert len(titles) == len(set(titles)), "duplicate titles"
    for release in data["releases"]:
        assert set(release) <= ALLOWED_KEYS
        assert release["title"].strip()


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_has_no_links_or_keys(path):
    assert not LEAK.search(path.read_text(encoding="utf-8"))
