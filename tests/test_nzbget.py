"""The NZBGet client against a fake. No network."""

import pytest

from fake_nzbget import FOREIGN_ID, NZBGET_PASS, NZBGET_URL, NZBGET_USER, FakeNzbget
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.nzbget import NzbgetClient

from fake_sab import make_nzb


def client(fake, password=NZBGET_PASS):
    return NzbgetClient(NZBGET_URL, NZBGET_USER, password, transport=fake, min_interval=0)


def test_check_version_and_category():
    fake = FakeNzbget()
    assert client(fake).check("mags") == "24.3"
    with pytest.raises(ClientError, match="does not exist"):
        client(fake).check("magazines")
    with pytest.raises(ClientError, match="username or password"):
        client(fake, password="wrong").version()


def test_add_with_category_priority_and_paused():
    fake = FakeNzbget()
    nzb = make_nzb("Linux 311.pdf")
    job = client(fake).add_nzb(nzb, "Linux 311", "mags", priority=50, paused=True)
    assert job == "1"
    added = fake.added[0]
    assert (added["category"], added["priority"], added["paused"], added["nzb"]) == (
        "mags",
        50,
        True,
        nzb,
    )
    with pytest.raises(ClientError):
        client(fake).add_nzb(nzb, "x", "")


def test_jobs_only_ours_and_states():
    fake = FakeNzbget()
    nz = client(fake)
    job = nz.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    [state] = nz.jobs([job], "mags")
    assert state.status is JobStatus.QUEUED
    assert nz.jobs([str(FOREIGN_ID)], "mags") == []  # another app's job never comes back
    fake.queue[int(job)]["Status"] = "UNPACKING"
    fake.queue[int(job)]["RemainingSizeMB"] = 20
    [state] = nz.jobs([job, str(FOREIGN_ID)], "mags")
    assert state.status is JobStatus.DOWNLOADING and state.progress == 75.0
    fake.finish(job)
    [state] = nz.jobs([job], "mags")
    assert (
        state.status is JobStatus.COMPLETED
        and state.storage_path == "/data/complete/mags/Linux 311"
    )
    fake.history[int(job)]["Status"] = "FAILURE/PAR"
    [state] = nz.jobs([job], "mags")
    assert state.status is JobStatus.FAILED and state.fail_message == "FAILURE/PAR"


def test_remove_only_ours():
    fake = FakeNzbget()
    nz = client(fake)
    job = nz.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    assert nz.remove(str(FOREIGN_ID), "mags") is False
    assert nz.remove(job, "mags") is True
    assert fake.removed == [("GroupFinalDelete", [int(job)])]
    job2 = nz.add_nzb(make_nzb("b.pdf"), "Linux 312", "mags")
    fake.finish(job2)
    assert nz.remove(job2, "mags") is True and fake.removed[-1] == ("HistoryDelete", [int(job2)])
    assert FOREIGN_ID in fake.queue


def test_password_is_redacted():
    assert NZBGET_PASS not in client(FakeNzbget()).redact(f"error with {NZBGET_PASS}")
