"""Usenet and Torrent Blackhole against real temporary folders."""

import os

import pytest

from fake_qbit import make_torrent
from periodicool.bencode import info_hash
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.blackhole import BlackholeClient

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
