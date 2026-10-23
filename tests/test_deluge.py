"""The Deluge client against a fake. No network."""

import pytest

from fake_deluge import DE_PASS, DE_URL, FOREIGN_HASH, FakeDeluge
from fake_qbit import make_torrent
from periodicool.bencode import info_hash
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.deluge import DelugeClient


def client(fake, password=DE_PASS):
    return DelugeClient(DE_URL, password, transport=fake, min_interval=0)


def test_login_connect_and_check():
    fake = FakeDeluge()
    assert client(fake).check("mags") == "2.1.1"
    assert fake.calls[:5] == [
        "auth.login",
        "web.connected",
        "web.get_hosts",
        "web.connect",
        "web.connected",
    ]
    with pytest.raises(ClientError, match="does not exist"):
        client(FakeDeluge()).check("magazines")
    with pytest.raises(ClientError, match="refused the password"):
        client(FakeDeluge(), password="nope").version()
    with pytest.raises(ClientError, match="Label plugin is off"):
        client(FakeDeluge(label_plugin=False)).check("mags")


def test_add_sets_label_ratio_and_top():
    fake = FakeDeluge()
    data = make_torrent()
    job = client(fake).add_torrent(
        "Linux 311",
        "MAGS",
        torrent=data,
        seed_ratio=2,
        priority="first",
        paused=True,
        directory="/data/mags",
    )
    assert job == info_hash(data)
    t = fake.torrents[job]
    assert t["label"] == "mags" and t["added_options"] == {
        "add_paused": True,
        "download_location": "/data/mags",
    }
    assert fake.options[job] == {"stop_at_ratio": True, "stop_ratio": 2.0} and fake.top == [job]
    magnet = "magnet:?xt=urn:btih:" + "c" * 40
    assert client(fake).add_torrent("x", "mags", magnet=magnet) == "c" * 40


def test_jobs_only_ours_and_states():
    fake = FakeDeluge()
    de = client(fake)
    job = de.add_torrent("Linux 311", "mags", torrent=make_torrent())
    [state] = de.jobs([job], "mags")
    assert state.status is JobStatus.DOWNLOADING
    assert de.jobs([FOREIGN_HASH], "mags") == []
    fake.set_state(job, "Seeding", 100.0)
    [state] = de.jobs([job], "mags")
    assert state.status is JobStatus.COMPLETED and state.storage_path == "/data/torrents/Linux 311"
    fake.set_state(job, "Error", 40.0, "Move storage error")
    [state] = de.jobs([job], "mags")
    assert state.status is JobStatus.FAILED and state.fail_message == "Move storage error"


def test_remove_keeps_files_and_only_ours():
    fake = FakeDeluge()
    de = client(fake)
    job = de.add_torrent("Linux 311", "mags", torrent=make_torrent())
    assert de.remove(FOREIGN_HASH, "mags") is False
    assert de.remove(job, "mags") is True
    assert fake.removed == [(job, False)] and FOREIGN_HASH in fake.torrents
