"""Commands started through the REST API (Sonarr's POST /command): run on one worker
thread, through the same runners as System, Tasks, Run now, and kept in memory so a
caller can follow them. Only the last few are kept; nothing is stored."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# API name -> job name in jobs.RUNNERS. MagazineSearch is handled on its own.
JOBS = {
    "RssSync": "rss",
    "MissingSearch": "wanted",
    "RescanLibrary": "library",
    "ImportFolder": "import",
    "CheckDownloads": "downloads",
    "Backup": "backup",
    "HealthCheck": "health",
}
NAMES = (*JOBS, "MagazineSearch")
KEEP = 50
WAIT_FOR_LOCK_S = 3600


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Command:
    id: int
    name: str
    magazine_id: int | None = None
    status: str = "queued"  # queued, started, completed, failed
    message: str = ""
    queued: datetime = field(default_factory=_now)
    started: datetime | None = None
    ended: datetime | None = None

    def as_json(self) -> dict:
        def iso(value):
            return value.isoformat(timespec="seconds") if value else None

        return {
            "id": self.id,
            "name": self.name,
            "magazineId": self.magazine_id,
            "status": self.status,
            "message": self.message,
            "queued": iso(self.queued),
            "started": iso(self.started),
            "ended": iso(self.ended),
        }


class Commands:
    def __init__(self, scheduler_factory, search_runner) -> None:
        """`scheduler_factory()` gives a jobs.Scheduler; `search_runner(magazine_id)` runs
        a magazine search and returns a message (raises on failure)."""
        self._scheduler = scheduler_factory
        self._search = search_runner
        self._ids = itertools.count(1)
        self._records: OrderedDict[int, Command] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="periodicool-api", daemon=True)
        self._thread.start()

    def submit(self, name: str, magazine_id: int | None = None) -> Command:
        with self._lock:
            cmd = Command(next(self._ids), name, magazine_id)
            self._records[cmd.id] = cmd
            while len(self._records) > KEEP:
                self._records.popitem(last=False)
        self._queue.put(cmd)
        return cmd

    def get(self, command_id: int) -> Command | None:
        with self._lock:
            return self._records.get(command_id)

    def recent(self) -> list[Command]:
        with self._lock:
            return list(reversed(self._records.values()))

    def stop(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def wait(self, command_id: int, timeout: float = 10.0) -> Command | None:
        """Until the command has ended (tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            cmd = self.get(command_id)
            if cmd is None or cmd.status in ("completed", "failed"):
                return cmd
            time.sleep(0.02)
        return self.get(command_id)

    def _run(self) -> None:
        while True:
            cmd = self._queue.get()
            if cmd is None:
                return
            cmd.status, cmd.started = "started", _now()
            try:
                if cmd.name == "MagazineSearch":
                    message = self._search(cmd.magazine_id)
                else:
                    message = self._run_job(JOBS[cmd.name])
                failed = message.startswith("error")
                cmd.status, cmd.message = ("failed" if failed else "completed"), message
            except Exception as exc:  # noqa: BLE001  a command reports, never crashes the worker
                log.exception("API command %s failed", cmd.name)
                cmd.status, cmd.message = "failed", str(exc)[:500]
            cmd.ended = _now()

    def _run_job(self, job: str) -> str:
        scheduler = self._scheduler()
        waited = 0.0
        while (result := scheduler.run_now(job)) is None:  # another task is running
            if waited >= WAIT_FOR_LOCK_S:
                return "error: another task kept running for an hour"
            time.sleep(1)
            waited += 1
        return result
