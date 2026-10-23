"""The rTorrent client against a fake. No network."""

import pytest

from fake_qbit import make_torrent
from fake_rtorrent import FOREIGN_HASH, RT_PASS, RT_URL, RT_USER, FakeRtorrent
from periodicool.bencode import info_hash
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.rtorrent import RtorrentClient


def client(fake, password=RT_PASS):
    return RtorrentClient(RT_URL, RT_USER, password, transport=fake, min_interval=0)


def test_version_and_check():
    fake = FakeRtorrent()
    assert client(fake).check("mags") == "0.9.8"
    with pytest.raises(ClientError, match="label"):
        client(fake).check("")
    with pytest.raises(ClientError, match="username or password"):
        client(fake, password="nope").version()


def test_add_with_label_directory_priority_and_stopped():
    fake = FakeRtorrent()
    data = make_torrent()
    job = client(fake).add_torrent(
        "Linux 311", "mags", torrent=data, priority="3", paused=True, directory="/data/mags"
    )
    assert job == info_hash(data)
    name, _ = fake.calls[-1]
    assert name == "load.raw"
    assert fake.torrents[job.upper()]["commands"] == [
        "d.custom1.set=mags",
        'd.directory.set="/data/mags"',
        "d.priority.set=3",
    ]
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    assert client(fake).add_torrent("x", "mags", magnet=magnet) == "a" * 40
    assert fake.calls[-1][0] == "load.start"
    with pytest.raises(ClientError, match="not NZB"):
        client(fake).add_nzb(b"<nzb/>", "x", "mags")


def test_jobs_only_ours_and_states():
    fake = FakeRtorrent()
    rt = client(fake)
    job = rt.add_torrent("Linux 311", "mags", torrent=make_torrent())
    [state] = rt.jobs([job], "mags")
    assert state.status is JobStatus.DOWNLOADING and state.progress == 40.0
    assert rt.jobs([FOREIGN_HASH], "mags") == []
    assert rt.jobs(["0" * 40], "mags") == []
    fake.torrents[job.upper()].update({"d.state": 0})
    assert rt.jobs([job], "mags")[0].status is JobStatus.QUEUED
    fake.torrents[job.upper()].update({"d.complete": 1, "d.left_bytes": 0})
    [state] = rt.jobs([job], "mags")
    assert state.status is JobStatus.COMPLETED and state.storage_path == "/data/torrents/Linux 311"


def test_remove_keeps_files_and_only_ours():
    fake = FakeRtorrent()
    rt = client(fake)
    job = rt.add_torrent("Linux 311", "mags", torrent=make_torrent())
    assert rt.remove(FOREIGN_HASH, "mags") is False
    assert rt.remove(job, "mags") is True
    assert fake.erased == [job.upper()] and FOREIGN_HASH.upper() in fake.torrents


def test_label_cannot_break_out_of_the_command():
    fake = FakeRtorrent()
    client(fake).add_torrent("x", 'mags";d.erase=', torrent=make_torrent())
    assert fake.calls[-1][1][2] == "d.custom1.set=magsd.erase="
