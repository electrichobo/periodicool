"""The download client kinds: fields, defaults, addresses, and the Settings pop-ups."""

import pytest
from sqlalchemy import select

from periodicool.clients import registry
from periodicool.models import DownloadClient

from test_web_magazines import session, toast  # noqa: F401


@pytest.mark.parametrize(
    ("settings", "url"),
    [
        ({"host": "sab", "port": 8080}, "http://sab:8080"),
        ({"host": "sab", "port": 443, "use_ssl": True}, "https://sab"),
        (
            {"host": "nas", "port": 9091, "url_base": "/transmission/"},
            "http://nas:9091/transmission",
        ),
        ({"host": "nas", "port": 8080, "url_base": "RPC2"}, "http://nas:8080/RPC2"),
        ({"host": "::1", "port": 8112}, "http://[::1]:8112"),
        ({"host": ""}, ""),
    ],
)
def test_compose_url(settings, url):
    assert registry.compose_url(settings) == url
    if url:
        back = registry.split_url(url)
        assert registry.compose_url(back) == url


def test_every_kind_has_the_basics():
    assert set(registry.KINDS) == {
        "sabnzbd",
        "nzbget",
        "qbittorrent",
        "transmission",
        "deluge",
        "rtorrent",
        "usenet_blackhole",
        "torrent_blackhole",
    }
    for kind in registry.KINDS.values():
        names = [f.name for f in kind.fields]
        if kind.local:
            assert names[:2] == ["drop_folder", "watch_folder"] and not kind.has_category
            continue
        assert names[:4] == ["host", "port", "use_ssl", "url_base"], kind.kind
        assert "category" in names and "recent_priority" in names and "older_priority" in names
        assert kind.protocol in ("usenet", "torrent") and kind.info
        assert registry.defaults(kind.kind)["port"]


def test_picker_groups_usenet_and_torrents(client):
    page = client.get("/settings/downloads/new").text
    usenet = page[page.index(">Usenet<") : page.index(">Torrents<")]
    assert "?kind=sabnzbd" in usenet and "?kind=nzbget" in usenet and "qbittorrent" not in usenet
    assert "?kind=usenet_blackhole" in usenet
    torrents = page[page.index(">Torrents<") :]
    for kind in ("qbittorrent", "transmission", "deluge", "rtorrent", "torrent_blackhole"):
        assert f"?kind={kind}" in torrents
    assert page.count("More info") == 8


@pytest.mark.parametrize("kind", list(registry.KINDS))
def test_each_pop_up_shows_its_fields_with_defaults(client, kind):
    page = client.get(f"/settings/downloads/new?kind={kind}").text
    spec = registry.KINDS[kind]
    for f in spec.fields:
        assert f'name="{f.name}"' in page, f.name
    if spec.local:
        assert 'name="host"' not in page and 'name="category"' not in page
        assert "data-browse" in page
        return
    port = registry.defaults(kind)["port"]
    assert f'value="{port}"' in page


def test_save_composes_the_address_and_keeps_secrets_write_only(client):
    base = {
        "kind": "sabnzbd",
        "name": "SAB",
        "host": "sab",
        "port": "8080",
        "url_base": "/sabnzbd",
        "api_key": "sab-api-key-1",
        "password": "sab-pass-1",
        "category": "mags",
        "recent_priority": "1",
        "older_priority": "-1",
        "remove_completed": "on",
        "priority": "1",
        "enabled": "on",
    }
    bad = client.post("/settings/downloads/save", data=base | {"host": "http://sab:8080"})
    assert "Only the host name" in bad.text
    assert client.post("/settings/downloads/save", data=base).status_code == 204
    with session(client) as s:
        row = s.scalars(select(DownloadClient)).one()
        assert row.url == "http://sab:8080/sabnzbd" and row.api_key == "sab-api-key-1"
        assert row.password == "sab-pass-1"
        assert row.settings["recent_priority"] == "1" and row.settings["remove_completed"] is True
        row_id = row.id
    edit = client.get(f"/settings/downloads/{row_id}/edit").text
    assert "sab-api-key-1" not in edit and "sab-pass-1" not in edit
    assert 'value="/sabnzbd"' in edit and "API key set" in edit and "Password set" in edit
    # Empty secret fields keep the saved ones; unticked switches are off.
    keep = base | {"id": str(row_id), "api_key": "", "password": "", "port": "9090"}
    del keep["remove_completed"]
    assert client.post("/settings/downloads/save", data=keep).status_code == 204
    with session(client) as s:
        row = s.get(DownloadClient, row_id)
        assert (row.api_key, row.password, row.url) == (
            "sab-api-key-1",
            "sab-pass-1",
            "http://sab:9090/sabnzbd",
        )
        assert row.settings["remove_completed"] is False


def test_a_single_address_still_works(client):
    data = {
        "kind": "transmission",
        "name": "Transmission",
        "url": "https://nas:9091/transmission",
        "category": "mags",
        "priority": "1",
        "enabled": "on",
    }
    assert client.post("/settings/downloads/save", data=data).status_code == 204
    with session(client) as s:
        row = s.scalars(select(DownloadClient)).one()
        assert row.protocol == "torrent" and row.url == "https://nas:9091/transmission"
        assert row.settings["use_ssl"] is True and row.settings["host"] == "nas"
