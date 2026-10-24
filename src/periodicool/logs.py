"""Logging: a rotating text file in /config/logs and the event table shown in the UI.

Both get the same redaction, so API keys and secrets never reach either. Database
writes happen on a background thread: a log call made while a request or job holds
SQLite's write lock must not wait on that same lock.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import queue
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from periodicool.models import LogEvent

LOGGER = "periodicool"
LOG_DIR = "logs"
FOLDER_KEY = "log.folder"
# A folder chosen in System, Log files; loaded at start and on save. None: /config/logs.
FOLDER: dict[str, str | None] = {"path": None}


def log_dir(config_dir: Path) -> Path:
    return Path(FOLDER["path"]) if FOLDER["path"] else config_dir / LOG_DIR


LOG_FILE = "periodicool.txt"
FILE_BYTES = 1_000_000
FILE_COUNT = 5
KEEP_DAYS = 30
KEEP_ROWS = 20_000
# Environment values that are secrets; their values are scrubbed from every message.
SECRET_ENVS = ("PROWLARR_API_KEY", "SABNZBD_API_KEY", "PERIODICOOL_SECRET")

_PATTERNS = [
    re.compile(r"(?i)(apikey=|api_key=|x-api-key:\s*|token=|password=)[^&\s\"']+"),
]


class Redactor(logging.Filter):
    """Scrubs secrets from the formatted message before any handler sees it."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def learn(self, secrets) -> None:
        """Add secrets saved after start (keys typed into Settings)."""
        for secret in secrets:
            if secret and len(secret) >= 6 and secret not in self.secrets:
                self.secrets.append(secret)

    def redact(self, text: str) -> str:
        for pattern in _PATTERNS:
            text = pattern.sub(r"\1REDACTED", text)
        for secret in self.secrets:
            text = text.replace(secret, "REDACTED")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


def known_secrets(config_dir: Path) -> list[str]:
    found = [os.environ.get(name, "") for name in SECRET_ENVS]
    for name in ("secret.key", "api.key"):
        with contextlib.suppress(OSError):
            found.append((config_dir / name).read_text(encoding="ascii").strip())
    return [s for s in found if s]


def stored_secrets(session) -> list[str]:
    """Keys and passwords saved with the indexer, download client and webhook entries
    (a webhook's header values count: they often carry tokens)."""
    from periodicool.models import DownloadClient, Indexer, Notification

    found = list(session.scalars(select(Indexer.api_key)))
    for key, password in session.execute(select(DownloadClient.api_key, DownloadClient.password)):
        found += [key, password]
    for password, settings in session.execute(select(Notification.password, Notification.settings)):
        found += [password, *(value for _, value in (settings or {}).get("headers") or [])]
    return [s for s in found if s]


class DbLogHandler(logging.Handler):
    """Queues records; a writer thread inserts them in batches and prunes old rows."""

    def __init__(self, session_factory, level=logging.INFO) -> None:
        super().__init__(level)
        self._sessions = session_factory
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="periodicool-log", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        self._queue.put(
            (
                datetime.fromtimestamp(record.created, UTC),
                record.levelname,
                record.name[:100],
                record.getMessage()[:4000],
            )
        )

    def _run(self) -> None:
        writes = 0
        while True:
            item = self._queue.get()
            if item is None:
                return
            batch = [item]
            while True:
                try:
                    more = self._queue.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self._write(batch)
                    return
                batch.append(more)
            self._write(batch)
            writes += 1
            if writes % 50 == 1:
                self._prune()

    def _write(self, batch) -> None:
        try:
            with self._sessions() as session:
                session.add_all(
                    LogEvent(time=t, level=lvl, logger=name, message=msg)
                    for t, lvl, name, msg in batch
                )
                session.commit()
        except Exception:  # noqa: BLE001  never let logging break the app
            pass

    def _prune(self) -> None:
        try:
            with self._sessions() as session:
                cutoff = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
                session.execute(delete(LogEvent).where(LogEvent.time < cutoff))
                count = session.scalar(select(func.count()).select_from(LogEvent)) or 0
                if count > KEEP_ROWS:
                    keep_from = session.scalar(
                        select(LogEvent.id).order_by(LogEvent.id.desc()).offset(KEEP_ROWS - 1)
                    )
                    session.execute(delete(LogEvent).where(LogEvent.id < keep_from))
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)
        super().close()


class LogSetup:
    """Attaches the file and database handlers to the periodicool logger; detach() undoes it."""

    def __init__(self, config_dir: Path, session_factory, level: str = "info") -> None:
        self.logger = logging.getLogger(LOGGER)
        self.redactor = Redactor(known_secrets(config_dir))
        self.config_dir = config_dir
        folder = log_dir(config_dir)
        folder.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.file = logging.handlers.RotatingFileHandler(
            folder / LOG_FILE, maxBytes=FILE_BYTES, backupCount=FILE_COUNT, encoding="utf-8"
        )
        self.file.setFormatter(fmt)
        self._fmt = fmt
        self.db = DbLogHandler(session_factory)
        self._old_level = self.logger.level
        self.base_level = getattr(logging, level.upper(), logging.INFO)
        self.logger.setLevel(self.base_level)
        self.verbose = False
        # Filters on a logger skip records from its children, so each handler gets one.
        for handler in (self.file, self.db):
            handler.addFilter(self.redactor)
        self.logger.addHandler(self.file)
        self.logger.addHandler(self.db)

    def move_file(self) -> Path:
        """Write the log file in the folder now chosen (System, Log files)."""
        folder = log_dir(self.config_dir)
        folder.mkdir(parents=True, exist_ok=True)
        new = logging.handlers.RotatingFileHandler(
            folder / LOG_FILE, maxBytes=FILE_BYTES, backupCount=FILE_COUNT, encoding="utf-8"
        )
        new.setFormatter(self._fmt)
        new.setLevel(self.file.level)
        new.addFilter(self.redactor)
        old, self.file = self.file, new
        self.logger.addHandler(new)
        self.logger.removeHandler(old)
        old.close()
        return folder

    def set_verbose(self, on: bool) -> None:
        """Debug lines on or off, for the file, the event table and the console."""
        self.verbose = on
        self.logger.setLevel(logging.DEBUG if on else self.base_level)
        for handler in (self.file, self.db):
            handler.setLevel(logging.DEBUG if on else logging.INFO)

    def detach(self) -> None:
        for handler in (self.file, self.db):
            self.logger.removeHandler(handler)
            handler.close()
        self.logger.setLevel(self._old_level)
