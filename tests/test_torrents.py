"""Torrents: results from Prowlarr and Torznab, fetching, grabbing and importing. No network."""

import os

import pytest
from sqlalchemy import select

from fake_newznab import NZ_KEY, NZ_URL, FakeNewznab, items_xml
from fake_qbit import QB_PASS, QB_URL, QB_USER, FakeQbit, make_torrent
from periodicool import search
from periodicool.bencode import info_hash
from periodicool.models import Indexer, Magazine, Release
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


# Grab to import


def torrent_setup(e, tmp_dir):
    """A qBittorrent entry mapping its folder into the downloads folder, and one
    torrent release found through Prowlarr's torrent indexer."""
    from periodicool import downloads

    folder = e.settings.downloads_dir / "torrents"
    folder.mkdir(parents=True, exist_ok=True)
    row = with_qbit(e)
    row.path_map = [["/data/torrents", str(folder)]]
    row.settings = None
    e.session.flush()
    fake_q = FakeQbit()
    src = Sources(e.session, {"prowlarr": e.fake_p, "sab": e.fake_s, "qbit": fake_q}, ENV)
    for c in src.indexers + src.clients:
        c.min_interval = 0
    entry = e.session.scalars(select(Indexer).where(Indexer.kind == "prowlarr")).one()
    entry.settings = {"seed_ratio": 2, "seed_time_min": 1440, "min_seeders": 1}
    e.fake_p.search_results = [
        release_json(L311, indexer_id=3) | {"protocol": "torrent", "seeders": 9, "leechers": 1}
    ]
    e.fake_p.nzb = make_torrent()
    search.search_magazine(e.session, src, linux(e), TODAY, force=True)
    rel = e.session.scalars(select(Release).where(Release.title == L311)).one()
    assert rel.protocol == "torrent" and rel.seeders == 9
    return downloads, src, fake_q, folder, rel


def test_torrent_grab_links_into_the_library_and_keeps_seeding(env, tmp_path):
    e = env
    downloads, src, fake_q, folder, rel = torrent_setup(e, tmp_path)
    res = downloads.grab(e.session, rel, src, src, TODAY)
    assert res.ok, res.reasons
    g = res.grab
    assert g.protocol == "torrent" and g.job_id == info_hash(make_torrent())
    form = fake_q.added[0]
    assert form["category"] == b"mags" and form["ratioLimit"] == b"2"
    assert form["seedingTimeLimit"] == b"1440"
    assert e.fake_s.added == []  # not sent to SABnzbd
    job_dir = folder / "Linux Magazine 311"
    job_dir.mkdir()
    (job_dir / "Linux Magazine 311.pdf").write_bytes(b"magazine")
    (job_dir / "cover.jpg").write_bytes(b"jpg")
    fake_q.set_state(g.job_id, "uploading", 1.0, "/data/torrents/Linux Magazine 311")
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status.value == "imported", g.error
    target = e.settings.library_dir / "Linux Magazine/2026/Linux Magazine - 2026-10.pdf"
    assert target.read_bytes() == b"magazine"
    # The torrent's own files stay where qBittorrent has them, folder and all.
    assert (job_dir / "Linux Magazine 311.pdf").read_bytes() == b"magazine"
    assert (job_dir / "cover.jpg").exists()
    assert target.samefile(job_dir / "Linux Magazine 311.pdf")  # a hard link
    assert fake_q.deleted == []  # never removed from qBittorrent


def test_torrent_import_copies_across_filesystems(env, tmp_path, monkeypatch):
    import errno

    e = env
    downloads, src, fake_q, folder, rel = torrent_setup(e, tmp_path)
    g = downloads.grab(e.session, rel, src, src, TODAY).grab
    job_dir = folder / "Linux Magazine 311"
    job_dir.mkdir()
    (job_dir / "Linux Magazine 311.pdf").write_bytes(b"magazine")
    fake_q.set_state(g.job_id, "stalledUP", 1.0, "/data/torrents/Linux Magazine 311")

    def other_disk(src_path, dst_path):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", other_disk)
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status.value == "imported", g.error
    target = e.settings.library_dir / "Linux Magazine/2026/Linux Magazine - 2026-10.pdf"
    assert target.read_bytes() == b"magazine"
    assert not target.samefile(job_dir / "Linux Magazine 311.pdf")
    assert (job_dir / "Linux Magazine 311.pdf").exists()
    from periodicool.models import FileMove

    assert e.session.scalars(select(FileMove.method)).all()[-1] == "copy"


def test_torrent_remove_keeps_files_and_bad_torrents_are_refused(env, tmp_path):
    from periodicool import downloads as dl

    e = env
    downloads, src, fake_q, folder, rel = torrent_setup(e, tmp_path)
    g = downloads.grab(e.session, rel, src, src, TODAY).grab
    dl.remove_grab(e.session, g.id, src)
    assert fake_q.deleted == [(g.job_id, "false")]
    assert dl.check_torrent(make_torrent(files=("x.nfo", "x.jpg"))).startswith("torrent holds no")
    assert dl.check_torrent(b"garbage") == "not a valid torrent file"
    assert dl.check_torrent(make_torrent()) is None


def test_no_torrent_client_no_torrent_grab(env):
    from periodicool import downloads

    e = env
    e.fake_p.search_results = [
        release_json(L311, indexer_id=1) | {"protocol": "torrent", "seeders": 9}
    ]
    src = sources(e)
    search.search_magazine(e.session, src, linux(e), TODAY, force=True)
    rel = e.session.scalars(select(Release).where(Release.title == L311)).one()
    assert not rel.accepted and "no enabled torrent download client" in rel.reasons
    res = downloads.grab(e.session, rel, src, src, TODAY)
    assert not res.ok and e.fake_s.added == []


def test_torrent_retry_import_keeps_the_source(env, tmp_path, monkeypatch):
    from periodicool import library as library_module

    e = env
    downloads, src, fake_q, folder, rel = torrent_setup(e, tmp_path)
    g = downloads.grab(e.session, rel, src, src, TODAY).grab
    job_dir = folder / "Linux Magazine 311"
    job_dir.mkdir()
    (job_dir / "Linux Magazine 311.pdf").write_bytes(b"magazine")
    fake_q.set_state(g.job_id, "uploading", 1.0, "/data/torrents/Linux Magazine 311")
    real_move = library_module._move

    def refused(src_path, dst, allow_copy, keep_source=False):
        raise PermissionError(13, "Permission denied", str(dst.parent))

    monkeypatch.setattr(library_module, "_move", refused)
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status.value == "review"
    monkeypatch.setattr(library_module, "_move", real_move)
    assert downloads.retry_import(e.session, g.id, e.settings, src, src, TODAY) == "imported"
    assert (job_dir / "Linux Magazine 311.pdf").read_bytes() == b"magazine"
