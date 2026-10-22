"""Background jobs: RSS sync, Wanted search, library rescan, import folder, backups.

A small scheduler inside the web app. Jobs run one at a time in a worker
thread, each with its own database session, and record their result in the
`jobs` table. `PERIODICOOL_JOBS=off` turns the scheduler off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from periodicool.config import Settings
from periodicool.downloads import auto_grab, make_download_client, monitor
from periodicool.library import (
    FIRST_SCAN_KEY,
    apply_scan,
    get_setting,
    plan_scan,
    process_import_folder,
    refresh_file_info,
)
from periodicool.models import Job, ScanKind
from periodicool.search import make_client, rss_sync, wanted_search

log = logging.getLogger(__name__)

DEFAULT_INTERVALS = {
    "rss": 15 * 60,
    "wanted": 6 * 3600,
    "library": 24 * 3600,
    "import": 5 * 60,
    "downloads": 60,
    "backup": 7 * 86400,
    "metadata": 30 * 86400,
}
LABELS = {
    "rss": "RSS sync",
    "wanted": "Search for wanted issues",
    "library": "Library rescan",
    "import": "Import folder",
    "downloads": "Check downloads",
    "backup": "Backup",
    "metadata": "Metadata lookups",
}
# One lock for every scheduler instance, so "Run now" never overlaps a scheduled run.
RUN_LOCK = threading.Lock()
TICK_SECONDS = 30


def today_in(tz: str, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(tz)).date()


def run_rss(session: Session, settings: Settings, now: datetime) -> str:
    client = make_client(session)
    if client is None:
        return "skipped: Prowlarr is not configured"
    today = today_in(settings.tz, now)
    r = rss_sync(session, client, today, now)
    text = f"{r.seen} seen, {r.new} new, {len(r.accepted)} accepted"
    return text + "".join(f"; {e}" for e in r.errors) + _auto_grab(session, client, today)


def _auto_grab(session: Session, prowlarr, today: date) -> str:
    grabs = auto_grab(session, prowlarr, make_download_client(session), today)
    sent = [g for g in grabs if g.ok]
    return f"; grabbed {len(sent)}" if grabs else ""


def run_wanted(session: Session, settings: Settings, now: datetime) -> str:
    client = make_client(session)
    if client is None:
        return "skipped: Prowlarr is not configured"
    today = today_in(settings.tz, now)
    results = wanted_search(session, client, today, now=now)
    if not results:
        return "nothing due" + _auto_grab(session, client, today)
    text = "; ".join(f"{r.magazine.title}: {r.new_accepted} new accepted" for r in results)
    return text + _auto_grab(session, client, today)


def run_downloads(session: Session, settings: Settings, now: datetime) -> str:
    client = make_download_client(session)
    if client is None:
        return "skipped: SABnzbd is not configured"
    r = monitor(session, client, settings, make_client(session), today_in(settings.tz, now))
    return (
        f"{r.updated} active, {len(r.imported)} imported, {len(r.failed)} failed, "
        f"{len(r.review)} need review, {len(r.removed)} removed, {len(r.retried)} retried"
    )


def run_library(session: Session, settings: Settings, now: datetime) -> str:
    if not settings.library_dir.is_dir():
        return f"skipped: library folder {settings.library_dir} not found"
    refresh_file_info(session, settings.library_dir)
    scan = plan_scan(session, settings.library_dir, ScanKind.LIBRARY)
    if not get_setting(session, FIRST_SCAN_KEY, False):
        return f"scan {scan.id} planned; waiting for the first scan to be applied by hand"
    result = apply_scan(session, scan.id, settings.library_dir)
    return f"scan {scan.id}: " + ", ".join(f"{k} {v}" for k, v in result.items())


def run_import(session: Session, settings: Settings, now: datetime) -> str:
    if settings.import_dir is None or not settings.import_dir.is_dir():
        return "skipped: no import folder"
    scan = process_import_folder(session, settings.import_dir, settings.library_dir)
    return f"scan {scan.id}: " + ", ".join(f"{k} {v}" for k, v in scan.counts.items())


def run_backup(session: Session, settings: Settings, now: datetime) -> str:
    from periodicool.backup import create_backup

    session.commit()  # the copy reads the database file, so write pending changes first
    made = create_backup(settings.config_dir, keep=int(get_setting(session, "backup.keep", 7)))
    return f"made {made.name}"


MAX_LOOKUPS_PER_RUN = 20


def run_metadata(session: Session, settings: Settings, now: datetime) -> str:
    """Refresh cached lookups for magazines with an ISSN. Off unless a source is on."""
    from sqlalchemy import select

    from periodicool.lookups import any_enabled
    from periodicool.lookups import service as lookups
    from periodicool.models import Magazine

    if not any_enabled(session):
        return "skipped: metadata lookups are off"
    asked = refreshed = 0
    for mag in session.scalars(select(Magazine).where(Magazine.issn.is_not(None))):
        for answer in lookups.records(session, mag).values():
            if not answer.off and not answer.cached:
                refreshed += 1
        asked += 1
        if refreshed >= MAX_LOOKUPS_PER_RUN:
            break
    pruned = lookups.prune(session)
    return f"{asked} magazines checked, {refreshed} lookups, {pruned} old answers removed"


RUNNERS: dict[str, Callable[[Session, Settings, datetime], str]] = {
    "rss": run_rss,
    "wanted": run_wanted,
    "library": run_library,
    "import": run_import,
    "downloads": run_downloads,
    "backup": run_backup,
    "metadata": run_metadata,
}


class Scheduler:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        runners: dict | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.clock = clock
        self.runners = runners or RUNNERS
        self._lock = RUN_LOCK

    def _job(self, session: Session, name: str) -> Job:
        job = session.get(Job, name)
        if job is None:
            interval = get_setting(session, f"jobs.{name}.interval_s", DEFAULT_INTERVALS[name])
            if name == "backup":
                interval = int(get_setting(session, "backup.interval_days", 7)) * 86400
            job = Job(name=name, enabled=True, interval_s=interval)
            session.add(job)
            session.commit()
        return job

    def run_due(self) -> dict[str, str]:
        """Run every job that is due, one after another. Returns their results."""
        ran: dict[str, str] = {}
        if not self._lock.acquire(blocking=False):
            return ran
        try:
            for name, runner in self.runners.items():
                with self.sessions() as session:
                    job = self._job(session, name)
                    now = self.clock()
                    nxt = job.next_run_at
                    if nxt is not None and nxt.tzinfo is None:
                        nxt = nxt.replace(tzinfo=UTC)
                    if not job.enabled or (nxt is not None and nxt > now):
                        continue
                    ran[name] = self._run(session, name, runner, now)
        finally:
            self._lock.release()
        return ran

    def _run(self, session: Session, name: str, runner, now: datetime) -> str:
        job = self._job(session, name)
        job.last_started_at = now
        try:
            result = runner(session, self.settings, now)
            session.commit()
        except Exception as exc:  # a failing job must not stop the others
            session.rollback()
            job = self._job(session, name)
            result = f"error: {exc}"
            log.exception("Job %s failed", name)
        job.last_finished_at = self.clock()
        job.last_result = result[:2000]
        job.next_run_at = now + timedelta(seconds=job.interval_s)
        session.commit()
        log.info("Job %s: %s", name, result)
        return result

    def run_now(self, name: str) -> str | None:
        """Run one job now (the Tasks page's Run now). None if another job is running."""
        runner = self.runners[name]
        if not self._lock.acquire(blocking=False):
            return None
        try:
            with self.sessions() as session:
                return self._run(session, name, runner, self.clock())
        finally:
            self._lock.release()

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.run_due)
            except Exception:
                log.exception("Scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
