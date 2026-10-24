"""Usenet and Torrent Blackhole against real temporary folders."""

import os

import pytest

from fake_qbit import make_torrent
from periodicool.bencode import info_hash
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.blackhole import BlackholeClient

from test_downloads import env as env  # noqa: F401  (fixture)

NOW = 1_800_000_000.0


@pytest.fixture
def folders(tmp_path):
    downloads = tmp_path / "downloads"
    drop, watch = downloads / "blackhole", downloads / "done"
    drop.mkdir(parents=True)
    watch.mkdir()
    return downloads, drop, watch


def hole(folders, protocol="usenet", **kw):
    downloads, drop, watch = folders
    clock = kw.pop("clock", lambda: NOW)
    return BlackholeClient(protocol, str(drop), str(watch), downloads, clock=clock, **kw)


def settle(path, when=NOW - 60):
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (when, when))


def test_check(folders, tmp_path):
    assert hole(folders).check() == "folders ready"
    downloads, drop, watch = folders
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ClientError, match="outside the downloads folder"):
        BlackholeClient("usenet", str(drop), str(outside), downloads).check()
    with pytest.raises(ClientError, match="does not exist"):
        BlackholeClient("usenet", str(drop / "nope"), str(watch), downloads).check()


def test_nzb_is_written_whole_and_never_overwritten(folders):
    _, drop, _ = folders
    bh = hole(folders)
    job = bh.add_nzb(b"<nzb/>", "Make: Volume 92", "")
    assert job == "Make - Volume 92"
    assert (drop / "Make - Volume 92.nzb").read_bytes() == b"<nzb/>"
    assert [p.name for p in drop.iterdir()] == ["Make - Volume 92.nzb"]  # no temp left
    with pytest.raises(ClientError, match="already in the drop folder"):
        bh.add_nzb(b"<other/>", "Make: Volume 92", "")
    assert (drop / "Make - Volume 92.nzb").read_bytes() == b"<nzb/>"
    with pytest.raises(ClientError, match="not torrents"):
        bh.add_torrent("x", torrent=make_torrent())


def test_torrent_named_from_its_info_and_magnets(folders):
    _, drop, _ = folders
    data = make_torrent(name="Linux Magazine 311")
    job = hole(folders, "torrent").add_torrent("whatever", torrent=data)
    assert job == f"{info_hash(data)}|Linux Magazine 311"
    assert (drop / "Linux Magazine 311.torrent").read_bytes() == data
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Linux+Magazine+312"
    with pytest.raises(ClientError, match="Save magnet files"):
        hole(folders, "torrent").add_torrent("x", magnet=magnet)
    saving = hole(folders, "torrent", save_magnets=True)
    assert saving.add_torrent("x", magnet=magnet) == "a" * 40 + "|Linux Magazine 312"
    assert (drop / "Linux Magazine 312.magnet").read_text() == magnet
    with pytest.raises(ClientError, match="no name"):
        saving.add_torrent("x", magnet="magnet:?xt=urn:btih:" + "b" * 40)


def test_jobs_follow_the_item_and_ignore_others(folders):
    _, drop, watch = folders
    bh = hole(folders)
    job = bh.add_nzb(b"<nzb/>", "Linux Magazine 311", "")
    (watch / "Some Movie").mkdir()  # not ours: never reported
    [state] = bh.jobs([job])
    assert state.status is JobStatus.QUEUED and "waiting" in state.client_status
    (drop / "Linux Magazine 311.nzb").unlink()  # picked up
    assert bh.jobs([job])[0].status is JobStatus.DOWNLOADING
    item = watch / "linux magazine 311"
    item.mkdir()
    (item / "Linux311.pdf").write_bytes(b"magazine")
    (item / "Linux311.pdf.part").write_bytes(b"x")
    settle(item)
    assert bh.jobs([job])[0].status is JobStatus.DOWNLOADING  # a partial file
    (item / "Linux311.pdf.part").unlink()
    (item / "new.pdf").write_bytes(b"y")  # changed just now
    os.utime(item / "new.pdf", (NOW - 5, NOW - 5))
    assert bh.jobs([job])[0].status is JobStatus.DOWNLOADING
    settle(item)
    [state] = bh.jobs([job])
    assert state.status is JobStatus.COMPLETED and state.storage_path == str(item)
    assert state.size_bytes == len(b"magazine") + 1
    assert [s.job_id for s in bh.jobs([job, "Not Ours"])] == [job, "Not Ours"]
    assert bh.jobs(["Not Ours"])[0].status is JobStatus.DOWNLOADING  # never found, never failed


def test_single_file_item_matches_by_stem(folders):
    _, _, watch = folders
    bh = hole(folders, "torrent")
    job = bh.add_torrent("x", torrent=make_torrent(name="Linux Magazine 311.pdf"))
    target = watch / "Linux Magazine 311.pdf"
    target.write_bytes(b"pdf")
    settle(target)
    assert bh.jobs([job])[0].status is JobStatus.COMPLETED


def test_remove_only_our_waiting_drop_file(folders):
    _, drop, watch = folders
    bh = hole(folders)
    job = bh.add_nzb(b"<nzb/>", "Linux Magazine 311", "")
    (watch / "Linux Magazine 311").mkdir()
    assert bh.remove(job) is True
    assert not (drop / "Linux Magazine 311.nzb").exists()
    assert (watch / "Linux Magazine 311").exists()  # the watch folder is never touched
    assert bh.remove(job) is False


# From grab to library


def _blackhole_setup(e, kind):
    from sqlalchemy import select

    from periodicool.models import DownloadClient
    from periodicool.sources import Sources
    from test_torrents import ENV

    drop = e.settings.downloads_dir / "blackhole"
    watch = e.settings.downloads_dir / "done"
    drop.mkdir()
    watch.mkdir()
    for row in e.session.scalars(select(DownloadClient)):
        row.enabled = False
    e.session.add(
        DownloadClient(
            name="Hole",
            kind=kind,
            protocol="usenet" if kind == "usenet_blackhole" else "torrent",
            url="",
            category="",
            path_map=[],
            settings={"drop_folder": str(drop), "watch_folder": str(watch), "wait_seconds": 30},
        )
    )
    e.session.flush()
    src = Sources(e.session, {"prowlarr": e.fake_p}, ENV)
    for c in src.indexers:
        c.min_interval = 0
    return src, drop, watch


def test_usenet_blackhole_from_grab_to_library(env):
    from periodicool import downloads
    from periodicool.models import GrabStatus

    from test_downloads import L311, TARGET, TODAY, seen

    e = env
    src, drop, watch = _blackhole_setup(e, "usenet_blackhole")
    res = downloads.grab(e.session, seen(e, L311)[L311], src, src, TODAY)
    assert res.ok, res.reasons
    g = res.grab
    assert (drop / f"{g.job_id}.nzb").exists() and g.category == ""
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status is GrabStatus.QUEUED
    (drop / f"{g.job_id}.nzb").unlink()  # the downloader took it
    job_dir = watch / g.job_id
    job_dir.mkdir()
    (job_dir / "Linux311.pdf").write_bytes(b"magazine")
    (job_dir / "Linux311.nfo").write_bytes(b"x")
    settle(job_dir, when=__import__("time").time() - 120)
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status is GrabStatus.IMPORTED, g.error
    assert (e.settings.library_dir / TARGET).read_bytes() == b"magazine"
    assert not job_dir.exists() and watch.exists()  # leftovers go, the watch folder stays


def test_torrent_blackhole_links_and_keeps_the_source(env):
    from periodicool import downloads, search
    from periodicool.models import GrabStatus, Magazine, Release

    from fake_prowlarr import release_json
    from test_downloads import L311, TARGET, TODAY

    e = env
    src, drop, watch = _blackhole_setup(e, "torrent_blackhole")
    e.fake_p.search_results = [
        release_json(L311, indexer_id=3) | {"protocol": "torrent", "seeders": 9, "leechers": 1}
    ]
    e.fake_p.nzb = make_torrent()
    from sqlalchemy import select

    mag = e.session.scalars(select(Magazine).where(Magazine.title == "Linux Magazine")).one()
    search.search_magazine(e.session, src, mag, TODAY, force=True)
    rel = e.session.scalars(select(Release).where(Release.title == L311)).one()
    res = downloads.grab(e.session, rel, src, src, TODAY)
    assert res.ok, res.reasons
    g = res.grab
    assert g.job_id == f"{info_hash(make_torrent())}|Linux Magazine 311"
    assert (drop / "Linux Magazine 311.torrent").exists()
    item = watch / "Linux Magazine 311"
    item.mkdir()
    (item / "Linux Magazine 311.pdf").write_bytes(b"magazine")
    settle(item, when=__import__("time").time() - 120)
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status is GrabStatus.IMPORTED, g.error
    target = e.settings.library_dir / TARGET
    assert target.read_bytes() == b"magazine" and (item / "Linux Magazine 311.pdf").exists()
    # The same torrent again, under another name: refused by its hash.
    again = downloads.grab(e.session, rel, src, src, TODAY)
    assert not again.ok


def test_blackhole_form_checks_folders(client, tmp_path):
    settings = client.app.state.settings
    drop = settings.downloads_dir / "bh-drop"
    watch = settings.downloads_dir / "bh-done"
    drop.mkdir(parents=True)
    watch.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    base = {
        "kind": "usenet_blackhole",
        "name": "Hole",
        "enabled": "on",
        "priority": "5",
        "drop_folder": str(drop),
        "watch_folder": str(outside),
        "wait_seconds": "30",
    }
    bad = client.post("/settings/downloads/save", data=base)
    assert "inside the downloads folder" in bad.text
    rel = client.post("/settings/downloads/save", data=base | {"watch_folder": "done"})
    assert "Use a full path" in rel.text
    ok = client.post("/settings/downloads/test", data=base | {"watch_folder": str(watch)})
    assert "both folders are ready" in ok.headers["HX-Trigger"]
    saved = client.post("/settings/downloads/save", data=base | {"watch_folder": str(watch)})
    assert saved.status_code == 204
    page = client.get("/settings/downloads").text
    assert "bh-drop" in page and "No API key" not in page
