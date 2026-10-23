"""A client entry's add and after-import options, from grab to import. No network."""

from datetime import date

import pytest

from fake_deluge import DE_PASS, DE_URL, FakeDeluge
from fake_nzbget import FakeNzbget
from fake_qbit import FOREIGN_HASH, make_torrent
from fake_rtorrent import RT_PASS, RT_URL, RT_USER, FakeRtorrent
from fake_transmission import TR_PASS, TR_URL, TR_USER, FakeTransmission
from periodicool import downloads
from periodicool.clients.deluge import DelugeClient
from periodicool.clients.rtorrent import RtorrentClient
from periodicool.clients.transmission import TransmissionClient
from periodicool.models import GrabStatus
from test_torrents import torrent_setup

from test_downloads import L311, TODAY, env, finish, seen  # noqa: F401


class Entry:
    def __init__(self, **options):
        self.options = options


def test_recent_or_older_priority_by_cover_date():
    entry = Entry(recent_priority="first", older_priority="last")
    today = date(2026, 10, 23)
    assert downloads.add_options(entry, date(2026, 10, 15), today) == {"priority": "first"}
    assert downloads.add_options(entry, date(2026, 9, 1), today) == {"priority": "last"}
    assert downloads.add_options(entry, None, today) == {"priority": "first"}


def test_paused_force_start_and_directory():
    assert downloads.add_options(Entry(add_paused=True, directory="/d"), None, TODAY) == {
        "paused": True,
        "directory": "/d",
    }
    assert downloads.add_options(Entry(initial_state="pause"), None, TODAY) == {"paused": True}
    assert downloads.add_options(Entry(initial_state="force_start"), None, TODAY) == {
        "force_start": True
    }
    assert downloads.add_options(Entry(initial_state="start"), None, TODAY) == {}
    assert downloads.add_options(object(), None, TODAY) == {}


@pytest.mark.parametrize(
    "make",
    [
        lambda: TransmissionClient(
            TR_URL, TR_USER, TR_PASS, transport=FakeTransmission(), min_interval=0
        ),
        lambda: DelugeClient(DE_URL, DE_PASS, transport=FakeDeluge(), min_interval=0),
        lambda: RtorrentClient(RT_URL, RT_USER, RT_PASS, transport=FakeRtorrent(), min_interval=0),
    ],
)
def test_every_torrent_client_takes_every_option(make):
    extra = {"priority": "first", "paused": True, "force_start": True, "directory": "/data/mags"}
    assert make().add_torrent("Linux 311", "mags", torrent=make_torrent(), **extra)


def test_nzbget_takes_priority_and_paused():
    from fake_nzbget import NZBGET_PASS, NZBGET_URL, NZBGET_USER
    from periodicool.clients.nzbget import NzbgetClient

    fake = FakeNzbget()
    ng = NzbgetClient(NZBGET_URL, NZBGET_USER, NZBGET_PASS, transport=fake, min_interval=0)
    ng.add_nzb(b"<nzb/>", "Linux 311", "mags", priority="50", paused=True, directory="/x")
    assert fake.added[0]["priority"] == 50 and fake.added[0]["paused"] is True


def test_sab_priority_and_remove_completed(env):
    e = env
    e.sab.options = {"recent_priority": "1", "older_priority": "-1", "remove_completed": True}
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    assert e.fake_s.priorities == ["1"]
    finish(e, g, {"Linux311.pdf": b"magazine"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED
    # Only our history entry goes, and SABnzbd is told to keep files.
    assert e.fake_s.deleted == [("history", g.job_id, "0")]


def test_sab_keeps_history_by_default(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    assert e.fake_s.priorities == ["-100"]
    finish(e, g, {"Linux311.pdf": b"magazine"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED and e.fake_s.deleted == []


def test_qbit_queue_position_force_start_and_post_import_category(env, tmp_path):
    e = env
    downloads_mod, src, fake_q, folder, rel = torrent_setup(e, tmp_path)
    [qbit] = [c for c in src.clients if c.name == "qbittorrent"]
    qbit.options = {
        "recent_priority": "first",
        "initial_state": "force_start",
        "post_import_category": "mags-done",
    }
    g = downloads.grab(e.session, rel, src, src, TODAY).grab
    assert fake_q.queue_moves == [("topPrio", g.job_id), ("setForceStart", g.job_id)]
    job_dir = folder / "Linux Magazine 311"
    job_dir.mkdir()
    (job_dir / "Linux Magazine 311.pdf").write_bytes(b"magazine")
    fake_q.set_state(g.job_id, "uploading", 1.0, "/data/torrents/Linux Magazine 311")
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    assert g.status is GrabStatus.IMPORTED, g.error
    assert fake_q.recategorized == [(g.job_id, "mags-done")]
    assert fake_q.torrents[FOREIGN_HASH]["category"] == "movies"
    assert fake_q.deleted == [] and (job_dir / "Linux Magazine 311.pdf").exists()


def test_qbit_paused_add(env, tmp_path):
    e = env
    _, src, fake_q, _, rel = torrent_setup(e, tmp_path)
    [qbit] = [c for c in src.clients if c.name == "qbittorrent"]
    qbit.options = {"initial_state": "pause"}
    downloads.grab(e.session, rel, src, src, TODAY)
    assert fake_q.added[0]["paused"] == b"true" and fake_q.queue_moves == []


def test_post_import_category_only_moves_our_torrent():
    fake = FakeDeluge()
    de = DelugeClient(DE_URL, DE_PASS, transport=fake, min_interval=0)
    job = de.add_torrent("Linux 311", "mags", torrent=make_torrent())
    from fake_deluge import FOREIGN_HASH as DE_FOREIGN

    assert de.set_category(DE_FOREIGN, "mags", "done") is False
    assert de.set_category(job, "mags", "Done") is True
    assert fake.torrents[job]["label"] == "done" and fake.torrents[DE_FOREIGN]["label"] == "movies"
    rfake = FakeRtorrent()
    rt = RtorrentClient(RT_URL, RT_USER, RT_PASS, transport=rfake, min_interval=0)
    rjob = rt.add_torrent("Linux 311", "mags", torrent=make_torrent())
    assert rt.set_category("f" * 40, "mags", "done") is False
    assert rt.set_category(rjob, "mags", "done") is True
