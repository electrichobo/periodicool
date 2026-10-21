"""The download-client interface, so other clients (NZBGet) can be added later.

Rules every client must follow (CLAUDE.md): add jobs only in PeriodiCool's
category, and only read jobs by the IDs PeriodiCool created. No calls that
change other jobs, other categories or global settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"  # includes post-processing (verify, repair, unpack)
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class JobState:
    job_id: str
    name: str
    category: str
    status: JobStatus
    client_status: str
    progress: float = 0.0  # 0 to 100
    storage_path: str | None = None  # as the client sees it
    fail_message: str | None = None
    size_bytes: int | None = None
    size_left_bytes: int | None = None
    time_left_s: int | None = None
    in_history: bool = False  # finished (or failed) rather than still in the queue


class ClientError(Exception):
    pass


class DownloadClient(Protocol):
    name: str

    def check(self, category: str) -> str:
        """Raise ClientError unless the client is reachable and the category exists."""
        ...

    def add_nzb(self, nzb: bytes, name: str, category: str) -> str:
        """Add an NZB in our category. Returns the client's job ID."""
        ...

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        """State of our jobs only. Jobs that are not found are left out."""
        ...

    def remove(self, job_id: str, category: str) -> bool:
        """Remove one of our jobs, after checking it is in our category.

        Returns False when the job is not there (already gone).
        """
        ...
