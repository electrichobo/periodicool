"""Health checks for the System page. Each has an LED kind, a message and a fix hint.

Local checks are quick and run on every visit. Connection checks (Prowlarr, SABnzbd
and the clock) talk to the user's own services and run when asked.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from periodicool.config import Settings
from periodicool.library import FIRST_SCAN_KEY, get_setting, queue
from periodicool.models import Job

CLOCK_WARN_SECONDS = 120


@dataclass(frozen=True)
class Check:
    name: str
    kind: str  # ok, warn, bad, info
    message: str
    fix: str = ""

    @property
    def label(self) -> str:
        return {"ok": "OK", "warn": "Warning", "bad": "Problem", "info": "Note"}[self.kind]


def _folder(name: str, path, writable: bool, fix: str) -> Check:
    if path is None:
        return Check(name, "info", "Not set.", fix)
    if not path.is_dir():
        return Check(name, "bad", f"{path} does not exist.", fix)
    if writable and not os.access(path, os.W_OK):
        return Check(name, "bad", f"{path} is not writable by this user.", "Check PUID and PGID.")
    return Check(name, "ok", str(path))


def _downloads_folder(path) -> Check:
    """The download client's completed folder for our category. PeriodiCool needs
    to write here (to clean up after imports), and so does the download client."""
    name = "Downloads folder"
    if not path.is_dir():
        return Check(
            name,
            "bad",
            f"{path} does not exist.",
            "Mount the download client's completed folder for PeriodiCool's category.",
        )
    if not os.access(path, os.W_OK):
        st = path.stat()
        made_by_docker = st.st_uid == 0 and not st.st_mode & 0o022
        why = (
            " It belongs to root with no group or other write access, which is what "
            "Docker does when it creates a missing folder for a volume."
            if made_by_docker
            else ""
        )
        return Check(
            name,
            "bad",
            f"{path} is not writable by PeriodiCool.{why}",
            "Give it the same owner and permissions as the download client's other "
            "completed folders, so both the client and PeriodiCool can write there.",
        )
    return Check(name, "ok", str(path))


def local_checks(session: Session, settings: Settings, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(UTC)
    checks = [
        _folder("Config folder", settings.config_dir, True, "Mount a volume at /config."),
        _folder("Library folder", settings.library_dir, True, "Mount your library at /library."),
        _downloads_folder(settings.downloads_dir),
    ]
    if settings.import_dir is not None:
        checks.append(_folder("Import folder", settings.import_dir, True, "Mount it at /import."))

    if not get_setting(session, "prowlarr.url"):
        checks.append(Check("Prowlarr", "warn", "Not set up.", "Settings, Indexers."))
    elif not os.environ.get("PROWLARR_API_KEY"):
        checks.append(Check("Prowlarr", "bad", "No API key.", "Set PROWLARR_API_KEY."))
    if not get_setting(session, "sabnzbd.url"):
        checks.append(Check("SABnzbd", "warn", "Not set up.", "Settings, Download clients."))
    elif not os.environ.get("SABNZBD_API_KEY"):
        checks.append(Check("SABnzbd", "bad", "No API key.", "Set SABNZBD_API_KEY."))
    elif not get_setting(session, "sabnzbd.category"):
        checks.append(Check("SABnzbd", "bad", "No category set.", "Settings, Download clients."))

    until = get_setting(session, "prowlarr.backoff_until")
    if until:
        until_dt = datetime.fromisoformat(until) if isinstance(until, str) else None
        if until_dt and until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=UTC)
        if until_dt and until_dt > now:
            checks.append(
                Check(
                    "Indexer limits",
                    "warn",
                    f"Prowlarr asked to slow down; searches wait until {until_dt:%H:%M} UTC.",
                )
            )

    failed = [j for j in session.scalars(select(Job)) if (j.last_result or "").startswith("error")]
    for job in failed:
        checks.append(
            Check(f"Task {job.name}", "bad", job.last_result[:300], "See System, Events.")
        )

    waiting = len(queue(session))
    if waiting:
        checks.append(
            Check(
                "Manual import",
                "warn",
                f"{waiting} files wait for review.",
                "Library, Manual import.",
            )
        )
    if not get_setting(session, FIRST_SCAN_KEY, False):
        checks.append(
            Check(
                "Library scan",
                "info",
                "The first library scan has not been applied yet.",
                "Library: run a scan, read the preview, then apply.",
            )
        )
    from periodicool.lookups import any_enabled
    from periodicool.lookups.service import frequency_mismatch
    from periodicool.models import Magazine

    if not any_enabled(session):
        checks.append(
            Check(
                "Metadata lookups",
                "info",
                "Off. Turning them on helps recognize magazines and editions.",
                "Settings, Metadata (optional; each source is opt-in).",
            )
        )
    else:
        for mag in session.scalars(select(Magazine).where(Magazine.issn.is_not(None))):
            if note := frequency_mismatch(session, mag):
                checks.append(
                    Check(
                        f"Frequency of {mag.title}", "warn", note, "Check the magazine's schedule."
                    )
                )
    jobs = session.scalar(select(func.count()).select_from(Job)) or 0
    if not settings.jobs_enabled:
        checks.append(Check("Background tasks", "warn", "Turned off by PERIODICOOL_JOBS."))
    elif not jobs:
        checks.append(Check("Background tasks", "info", "No task has run yet."))
    return checks


def remote_date(url: str, timeout: float = 5.0) -> datetime | None:
    """The Date header of a plain GET to the service's address (no key sent)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            header = resp.headers.get("Date")
    except urllib.error.HTTPError as exc:
        header = exc.headers.get("Date") if exc.headers else None
    except OSError, ValueError:
        return None
    if not header:
        return None
    try:
        return parsedate_to_datetime(header)
    except TypeError, ValueError:
        return None


def clock_check(
    name: str, url: str | None, now: datetime | None = None, fetch=remote_date
) -> Check:
    if not url:
        return Check(f"Clock vs {name}", "info", f"{name} is not set up.")
    theirs = fetch(url)
    if theirs is None:
        return Check(f"Clock vs {name}", "warn", f"Could not read the time from {name}.")
    ours = now or datetime.now(UTC)
    skew = (ours - theirs).total_seconds()
    if abs(skew) > CLOCK_WARN_SECONDS:
        direction = "behind" if skew < 0 else "ahead of"
        return Check(
            f"Clock vs {name}",
            "bad",
            f"This machine's clock is {abs(skew) / 60:.0f} minutes {direction} {name}.",
            "Sync the host clock (NTP). Wrong clocks break dates, schedules and indexer limits.",
        )
    return Check(f"Clock vs {name}", "ok", f"Within {abs(skew):.0f} seconds.")
