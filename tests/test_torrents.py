"""Torrent results from Prowlarr and Torznab, and fetching torrents and magnets. No network."""

import pytest
from sqlalchemy import select

from fake_newznab import NZ_KEY, NZ_URL, FakeNewznab, items_xml
from fake_qbit import QB_PASS, QB_URL, QB_USER, FakeQbit, make_torrent
from periodicool import search
from periodicool.models import Indexer, Magazine
from periodicool.prowlarr import ProwlarrError, parse_newznab
from periodicool.sources import PROWLARR_KEY_ENV, SAB_KEY_ENV, Sources
from rows import add_client, add_indexer

from fake_prowlarr import KEY, URL, FakeProwlarr, release_json
from fake_sab import SAB_KEY
from test_downloads import L311, TODAY, env  # noqa: F401

ENV = {PROWLARR_KEY_ENV: KEY, SAB_KEY_ENV: SAB_KEY}
MAGNET = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=Linux"


def with_qbit(e):
    return add_client(
        e.session,
        QB_URL,
        name="qBit",
        kind="qbittorrent",
        protocol="torrent",
        username=QB_USER,
        password=QB_PASS,
        category="mags",
    )


def sources(e, fake_t=None):
    src = Sources(
        e.session,
        {"prowlarr": e.fake_p, "torznab": fake_t, "sab": e.fake_s, "qbit": FakeQbit()},
        ENV,
    )
    for c in src.indexers + src.clients:
        c.min_interval = 0
    return src


def linux(e) -> Magazine:
    return e.session.scalars(select(Magazine).where(Magazine.title == "Linux Magazine")).one()


# Parsing


def test_torznab_attributes_and_magnet_links():
    found, _ = parse_newznab(items_xml(["A", "B"], torznab=True), None, "torrent")
    assert [(f.protocol, f.seeders, f.peers) for f in found] == [
        ("torrent", 12, 15),
        ("torrent", 11, 14),
    ]
    assert found[0].download_url.startswith("magnet:?xt=urn:btih:")


def test_prowlarr_json_torrent_fields():
    from periodicool.prowlarr import ProwlarrClient

    row = release_json(L311) | {
        "protocol": "torrent",
        "seeders": 7,
        "leechers": 2,
        "downloadUrl": None,
        "magnetUrl": MAGNET,
    }
    f = ProwlarrClient(URL, KEY, transport=FakeProwlarr())._from_json(row)
    assert (f.protocol, f.seeders, f.peers, f.download_url) == ("torrent", 7, 2, MAGNET)


# Torrent indexers only with a torrent client


def test_prowlarr_torrent_indexers_need_a_torrent_client(env):
    e = env
    names = {lim.name for lim in search.indexer_limits(e.session, e.prowlarr)}
    assert "Torrents" not in names
    with_qbit(e)
    limits = {lim.name: lim for lim in search.indexer_limits(e.session, e.prowlarr)}
    assert limits["Torrents"].protocol == "torrent" and limits["Alpha"].protocol == "usenet"


def test_torznab_entry_skipped_without_a_torrent_client(env):
    e = env
    fake_t = FakeNewznab(torznab=True)
    fake_t.search_titles = [L311]
    add_indexer(e.session, NZ_URL, NZ_KEY, "Tracker", "torznab", protocol="torrent")
    e.fake_p.search_results = []
    search.search_magazine(e.session, sources(e, fake_t), linux(e), TODAY)
    assert fake_t.calls == []  # no API hits while no torrent client is set up
    with_qbit(e)
    result = search.search_magazine(e.session, sources(e, fake_t), linux(e), TODAY, force=True)
    rel = next(r for r, _ in result.candidates if r.title == L311)
    assert rel.protocol == "torrent" and rel.seeders == 12 and rel.peers == 15
    assert (
        rel.source_id
        == e.session.scalars(select(Indexer.id).where(Indexer.name == "Tracker")).one()
    )
    assert NZ_KEY not in (rel.download_url or "")


# Fetching


def test_fetch_download_kinds(env):
    e = env
    p = e.prowlarr
    url = f"{URL}/1/download?link=abc"
    assert p.fetch_download(url, 1)[0] == "nzb"
    e.fake_p.nzb = make_torrent()
    kind, body = p.fetch_download(url, 1)
    assert kind == "torrent" and body == make_torrent()
    e.fake_p.download_redirect = MAGNET
    assert p.fetch_download(url, 1) == ("magnet", MAGNET)
    e.fake_p.download_redirect = "https://indexer.test/get/1"
    e.fake_p.indexer_reply = (302, b"", {"Location": MAGNET})
    assert p.fetch_download(url, 1) == ("magnet", MAGNET)
    assert p.fetch_download(MAGNET) == ("magnet", MAGNET)
    e.fake_p.download_redirect = None
    e.fake_p.nzb = b"<html>login page</html>"
    with pytest.raises(ProwlarrError, match="neither an NZB nor a torrent"):
        p.fetch_download(url, 1)
    with pytest.raises(ProwlarrError, match="not on the configured"):
        p.fetch_download("https://elsewhere.test/1/download", 1)
    with pytest.raises(ProwlarrError):
        p.fetch_download("magnet:?dn=no-hash")
    # The usenet path still insists on an NZB.
    e.fake_p.nzb = make_torrent()
    with pytest.raises(ProwlarrError, match="not an NZB"):
        p.fetch_nzb(url, 1)
