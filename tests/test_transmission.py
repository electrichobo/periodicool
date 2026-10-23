"""The Transmission client against a fake. No network."""

import pytest

from fake_qbit import make_torrent
from fake_transmission import FOREIGN_HASH, TR_PASS, TR_URL, TR_USER, FakeTransmission
from periodicool.bencode import info_hash
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.transmission import TransmissionClient


def client(fake, password=TR_PASS):
    return TransmissionClient(TR_URL, TR_USER, password, transport=fake, min_interval=0)


def test_session_handshake_and_check():
    fake = FakeTransmission()
    assert client(fake).check("mags").startswith("4.0.5")
    assert [c["method"] for c in fake.calls] == ["session-get"]  # after one 409
    with pytest.raises(ClientError, match="label"):
        client(fake).check("")
    with pytest.raises(ClientError, match="username or password"):
        client(fake, password="nope").version()


def test_add_with_label_ratio_directory_paused_and_top():
    fake = FakeTransmission()
    data = make_torrent()
    job = client(fake).add_torrent(
        "Linux 311",
        "mags",
        torrent=data,
        seed_ratio=1.5,
        priority="first",
        paused=True,
        directory="/data/mags",
    )
    assert job == info_hash(data)
    t = fake.torrents[job]
    assert t["labels"] == ["mags"] and t["added"]["paused"] is True
    assert t["added"]["download-dir"] == "/data/mags"
    assert t["set"]["seedRatioLimit"] == 1.5 and fake.moved_top == [job]
    magnet = "magnet:?xt=urn:btih:" + "b" * 40
    assert client(fake).add_torrent("x", "mags", magnet=magnet) == "b" * 40
    with pytest.raises(ClientError, match="not NZB"):
        client(fake).add_nzb(b"<nzb/>", "x", "mags")


def test_jobs_only_ours_and_states():
    fake = FakeTransmission()
    tr = client(fake)
    job = tr.add_torrent("Linux 311", "mags", torrent=make_torrent())
    [state] = tr.jobs([job], "mags")
    assert state.status is JobStatus.DOWNLOADING
    assert tr.jobs([FOREIGN_HASH], "mags") == []
    assert all(s.job_id != FOREIGN_HASH for s in tr.jobs([job, FOREIGN_HASH], "mags"))
    fake.set_state(job, 6, 1.0)
    [state] = tr.jobs([job], "mags")
    assert state.status is JobStatus.COMPLETED and state.storage_path == "/data/torrents/Linux 311"
    fake.set_state(job, 0, 0.4)
    assert tr.jobs([job], "mags")[0].status is JobStatus.QUEUED
    fake.set_state(job, 0, 0.4, error=3, error_string="No data found")
    [state] = tr.jobs([job], "mags")
    assert state.status is JobStatus.FAILED and state.fail_message == "No data found"
    fake.set_state(job, 4, 0.4, error=2, error_string="tracker gone")
    assert tr.jobs([job], "mags")[0].status is JobStatus.DOWNLOADING  # a tracker error is not ours


def test_remove_keeps_files_and_only_ours():
    fake = FakeTransmission()
    tr = client(fake)
    job = tr.add_torrent("Linux 311", "mags", torrent=make_torrent())
    assert tr.remove(FOREIGN_HASH, "mags") is False
    assert tr.remove(job, "mags") is True
    assert fake.removed == [([job], False)] and FOREIGN_HASH in fake.torrents


def test_tracker_messages_are_not_failures():
    fake = FakeTransmission()
    tr = client(fake)
    job = tr.add_torrent("Linux 311", "mags", torrent=make_torrent())
    fake.set_state(job, 4, 0.4, error=2, error_string="tracker gone")
    assert tr.jobs([job], "mags")[0].fail_message is None
