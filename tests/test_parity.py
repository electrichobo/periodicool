"""History, the event log, file details, covers, and removing our own SABnzbd jobs."""

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from periodicool import downloads, logs
from periodicool.clients.sabnzbd import SabnzbdClient, _duration
from periodicool.library import apply_scan, plan_scan, refresh_file_info
from periodicool.models import (
    BlockedRelease,
    GrabStatus,
    History,
    HistoryEvent,
    Issue,
    IssueStatus,
    LogEvent,
    Magazine,
    ScanKind,
)

from fake_sab import FOREIGN_HISTORY, FOREIGN_QUEUE, SAB_KEY, SAB_URL, FakeSab, make_nzb
from test_downloads import L311, L311_ALT, TARGET, TODAY, env, finish, issue, seen  # noqa: F401
from test_library import lib, put, session  # noqa: F401


def events(s, **where):
    query = select(History).order_by(History.id)
    for name, value in where.items():
        query = query.where(getattr(History, name) == value)
    return [h.event for h in s.scalars(query)]


# History from downloads


def test_grab_and_import_write_history_and_file_details(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.size_bytes == int(80.5 * 1024 * 1024)
    finish(e, g, {"Linux311.pdf": b"magazine"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED
    # A download records "imported", not a separate "file moved".
    assert events(e.session) == [HistoryEvent.GRABBED, HistoryEvent.IMPORTED]
    grabbed, imported = e.session.scalars(select(History).order_by(History.id))
    assert grabbed.grab_id == g.id and grabbed.source_title == L311
    assert grabbed.data["job_id"] == g.job_id and grabbed.issue_key == "n311"
    assert imported.data["path"] == TARGET and imported.data["format"] == "pdf"
    i = issue(e)
    assert (i.file_size, i.file_format) == (len(b"magazine"), "pdf")
    assert i.imported_at is not None


def test_failure_writes_one_history_row_and_blocklists(env):
    e = env
    rels = seen(e, L311, L311_ALT)
    first = downloads.grab(e.session, rels[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, first, {}, status="Failed", msg="Repair failed")
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    rows = list(e.session.scalars(select(History).where(History.grab_id == first.id)))
    assert [h.event for h in rows] == [HistoryEvent.GRABBED, HistoryEvent.DOWNLOAD_FAILED]
    assert rows[1].data["blocklisted"] is True and "Repair failed" in rows[1].data["reason"]
    assert HistoryEvent.BLOCKLISTED not in events(e.session)
    assert e.session.scalars(select(BlockedRelease)).one()


def test_import_failure_and_review_events(env):
    e = env
    rels = seen(e, L311, L311_ALT)
    g = downloads.grab(e.session, rels[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, g, {"readme.txt": b"x"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert events(e.session, grab_id=g.id)[-1] is HistoryEvent.IMPORT_FAILED

    existing = e.settings.library_dir / TARGET
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already here")
    retry = e.session.scalars(
        select(History).where(History.event == HistoryEvent.GRABBED).order_by(History.id.desc())
    ).first()
    retry_grab = e.session.get(type(g), retry.grab_id)
    finish(e, retry_grab, {"Linux311.pdf": b"new"}, folder="alt")
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert events(e.session, grab_id=retry_grab.id)[-1] is HistoryEvent.IMPORT_REVIEW


def test_bad_nzb_records_blocklisted(env):
    e = env
    e.fake_p.nzb = make_nzb("cover.jpg")
    downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY)
    assert events(e.session) == [HistoryEvent.BLOCKLISTED]


def test_vanished_job_records_removed(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    e.fake_s.queue.clear()
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert events(e.session, grab_id=g.id)[-1] is HistoryEvent.DOWNLOAD_REMOVED


# History from the library


def test_library_moves_and_links_write_history_once(session, lib):  # noqa: F811
    put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"ft351")
    put(lib, "Fortean Times/2017/Fortean Times - 2017-04.pdf", b"ft352")
    apply_scan(session, plan_scan(session, lib, ScanKind.LIBRARY).id, lib)
    assert sorted(events(session)) == sorted([HistoryEvent.FILE_MOVED, HistoryEvent.FILE_LINKED])
    moved = session.scalars(select(History).where(History.event == HistoryEvent.FILE_MOVED)).one()
    assert moved.data["target"] == "Fortean Times/2017/Fortean Times - 2017-03.pdf"
    # A rescan finds both in place: nothing new to record.
    apply_scan(session, plan_scan(session, lib, ScanKind.LIBRARY).id, lib)
    assert len(events(session)) == 2
    ft = session.scalars(select(Issue).where(Issue.key == "2017-03")).one()
    assert (ft.file_size, ft.file_format) == (5, "pdf")


# File details and covers


def test_refresh_fills_old_rows_and_finds_covers(session, lib):  # noqa: F811
    path = put(lib, "Fortean Times/2017/Fortean Times - 2017-03.pdf", b"twelve bytes")
    mag = session.scalars(select(Magazine).where(Magazine.title == "Fortean Times")).one()
    old = Issue(
        magazine_id=mag.id,
        key="2017-03",
        file_path="Fortean Times/2017/Fortean Times - 2017-03.pdf",
    )
    old.status = IssueStatus.IMPORTED
    session.add(old)
    session.flush()
    before = tree_snapshot(lib)

    assert refresh_file_info(session, lib) == {"files": 1, "covers": 0}
    assert (old.file_size, old.file_format) == (12, "pdf")
    assert old.imported_at == datetime.fromtimestamp(path.stat().st_mtime, UTC)

    put(lib, "Fortean Times/folder.jpg", b"img")
    assert refresh_file_info(session, lib) == {"files": 0, "covers": 1}
    assert mag.cover_path == "Fortean Times/folder.jpg"
    (lib / "Fortean Times/folder.jpg").unlink()
    refresh_file_info(session, lib)
    assert mag.cover_path is None
    assert set(tree_snapshot(lib)) == set(before)  # read-only apart from our own test edits


def tree_snapshot(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_tags_default_to_empty(session):  # noqa: F811
    assert all(m.tags == [] for m in session.scalars(select(Magazine)))


# SABnzbd: sizes, time left, remove


@pytest.mark.parametrize(
    "raw,seconds",
    [
        ("0:10:00", 600),
        ("1:02:03", 3723),
        ("2:01:02:03", 2 * 86400 + 3723),
        ("", None),
        ("x", None),
    ],
)
def test_time_left(raw, seconds):
    assert _duration(raw) == seconds


def _sab():
    fake = FakeSab()
    return fake, SabnzbdClient(SAB_URL, SAB_KEY, transport=fake, min_interval=0)


def test_queue_reports_size_and_time_left():
    fake, client = _sab()
    job = client.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    fake.queue[job].update(mb="100", mbleft="25", timeleft="0:01:30", status="Downloading")
    [state] = client.jobs([job], "mags")
    assert (state.size_bytes, state.size_left_bytes, state.time_left_s) == (
        100 * 1024 * 1024,
        25 * 1024 * 1024,
        90,
    )
    assert not state.in_history


def test_remove_queue_job_deletes_partial_files():
    fake, client = _sab()
    job = client.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    assert client.remove(job, "mags") is True
    assert fake.deleted == [("queue", job, "1")] and job not in fake.queue


def test_remove_finished_job_keeps_its_files():
    fake, client = _sab()
    job = client.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    fake.complete(job, "/data/complete/mags/x")
    assert client.remove(job, "mags") is True
    assert fake.deleted == [("history", job, "0")]


@pytest.mark.parametrize("job", [FOREIGN_QUEUE["nzo_id"], FOREIGN_HISTORY["nzo_id"], "nope"])
def test_remove_never_touches_other_jobs(job):
    fake, client = _sab()
    assert client.remove(job, "mags") is False
    # Even asking with the other app's own category: our client still checks the job list.
    assert fake.deleted == []


def test_remove_job_in_other_category_is_refused():
    fake, client = _sab()
    job = client.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    assert client.remove(job, "movies") is False
    assert fake.deleted == [] and job in fake.queue


def test_remove_grab_marks_removed_and_can_blocklist(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    downloads.remove_grab(e.session, g.id, e.sab, add_to_blocklist=True)
    assert g.status is GrabStatus.REMOVED and issue(e).status is IssueStatus.WANTED
    assert e.fake_s.deleted == [("queue", g.job_id, "1")]
    assert e.session.scalars(select(BlockedRelease)).one().title == L311
    last = e.session.scalars(select(History).order_by(History.id.desc())).first()
    assert last.event is HistoryEvent.DOWNLOAD_REMOVED and last.data["blocklisted"] is True
    with pytest.raises(ValueError, match="nothing to remove"):
        downloads.remove_grab(e.session, g.id, e.sab)
    with pytest.raises(ValueError, match="no grab"):
        downloads.remove_grab(e.session, 9999, e.sab)


def test_remove_grab_refuses_imported(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, g, {"Linux311.pdf": b"magazine"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    with pytest.raises(ValueError, match="imported"):
        downloads.remove_grab(e.session, g.id, e.sab)
    assert e.fake_s.deleted == []


# Logs


def test_redactor_scrubs_keys_and_known_secrets():
    r = logs.Redactor(["supersecretvalue"])
    text = r.redact("GET /api?apikey=abc123&mode=queue X-Api-Key: zzz token=t0k supersecretvalue")
    assert "abc123" not in text and "zzz" not in text and "t0k" not in text
    assert "supersecretvalue" not in text and text.count("REDACTED") == 4


def _wait_for_rows(sessions, n, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with sessions() as s:
            if (s.scalar(select(func.count()).select_from(LogEvent)) or 0) >= n:
                return
        time.sleep(0.02)
    raise AssertionError("log rows did not arrive")


def test_log_setup_writes_file_and_table_redacted(settings, monkeypatch):
    from periodicool import db

    monkeypatch.setenv("SABNZBD_API_KEY", "sab-secret-key-456")
    settings.config_dir.mkdir(parents=True)
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    sessions = db.make_session_factory(engine)
    setup = logs.LogSetup(settings.config_dir, sessions)
    try:
        logging.getLogger("periodicool.test").info("calling %s", "http://sab/api?apikey=abc")
        logging.getLogger("periodicool.test").warning("key is sab-secret-key-456")
        logging.getLogger("periodicool.test").debug("not kept")
        _wait_for_rows(sessions, 2)
    finally:
        setup.detach()
    with sessions() as s:
        rows = list(s.scalars(select(LogEvent).order_by(LogEvent.id)))
    assert [r.level for r in rows] == ["INFO", "WARNING"]
    assert all("abc" not in r.message and "sab-secret" not in r.message for r in rows)
    text = (settings.config_dir / "logs" / "periodicool.txt").read_text(encoding="utf-8")
    assert "REDACTED" in text and "sab-secret" not in text and "not kept" not in text
    # Detached: nothing more is written.
    logging.getLogger("periodicool.test").warning("after detach")
    assert "after detach" not in (settings.config_dir / "logs" / "periodicool.txt").read_text()
    engine.dispose()


def test_log_rows_are_pruned(settings, monkeypatch):
    from periodicool import db

    monkeypatch.setattr(logs, "KEEP_ROWS", 3)
    settings.config_dir.mkdir(parents=True)
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    sessions = db.make_session_factory(engine)
    with sessions() as s:
        old = datetime.now(UTC) - timedelta(days=logs.KEEP_DAYS + 1)
        s.add(LogEvent(time=old, level="INFO", logger="periodicool", message="ancient"))
        for n in range(5):
            s.add(LogEvent(level="INFO", logger="periodicool", message=f"row {n}"))
        s.commit()
    handler = logs.DbLogHandler(sessions)
    handler._prune()
    handler.close()
    with sessions() as s:
        left = [r.message for r in s.scalars(select(LogEvent).order_by(LogEvent.id))]
    assert left == ["row 2", "row 3", "row 4"]
    engine.dispose()


def test_app_logs_to_its_table(client):
    logging.getLogger("periodicool.web").info("hello from the app")
    _wait_for_rows(client.app.state.sessions, 1)
