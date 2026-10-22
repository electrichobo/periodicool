"""SABnzbd client (stdlib only).

SABnzbd usually serves other apps too. This client only ever calls:
`version` and `get_cats` (read), `addfile` (into our category), `queue`
and `history` filtered to our category and our job IDs, and `delete` for one
of our job IDs after confirming it is in our category. Responses are
filtered again here, so a job that is not ours is never returned or removed.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable

from periodicool import USER_AGENT
from periodicool.clients.base import ClientError, JobState, JobStatus

log = logging.getLogger(__name__)

# (method, url, headers, body, timeout) -> (status, body)
Transport = Callable[[str, str, dict, bytes | None, float], tuple[int, bytes]]

_DOWNLOADING = {"downloading", "fetching", "grabbing", "propagating", "checking"}
_POST = {"verifying", "repairing", "extracting", "moving", "running", "queued", "quickcheck"}


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


class SabnzbdClient:
    name = "sabnzbd"

    def __init__(
        self,
        url: str,
        api_key: str,
        transport: Transport | None = None,
        timeout: float = 60.0,
        min_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not url or not api_key:
            raise ClientError("SABnzbd URL and API key are required")
        self.url = url.rstrip("/")
        self._key = api_key
        self._transport = transport or _urllib_transport
        self.timeout, self.min_interval = timeout, min_interval
        self._clock, self._sleep, self._last = clock, sleep, None

    def redact(self, text) -> str:
        text = re.sub(r"(apikey=)[^&\s\"']+", r"\1REDACTED", str(text), flags=re.IGNORECASE)
        return text.replace(self._key, "REDACTED")

    def _call(self, params: dict, body: bytes | None = None, content_type: str | None = None):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        query = urllib.parse.urlencode({**params, "output": "json", "apikey": self._key})
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if content_type:
            headers["Content-Type"] = content_type
        method = "POST" if body is not None else "GET"
        try:
            status, raw = self._transport(
                method, f"{self.url}/api?{query}", headers, body, self.timeout
            )
        except OSError as exc:
            raise ClientError(f"cannot reach SABnzbd: {self.redact(exc)}") from None
        finally:
            self._last = self._clock()
        log.debug("SABnzbd %s: %s", params.get("mode"), status)
        if not 200 <= status < 300:
            raise ClientError(f"SABnzbd returned {status}: {self.redact(raw[:200])}")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ClientError(f"bad JSON from SABnzbd: {self.redact(raw[:200])}") from exc
        if isinstance(data, dict) and data.get("status") is False:
            raise ClientError(f"SABnzbd error: {self.redact(data.get('error', 'unknown'))}")
        return data

    def version(self) -> str:
        return str(self._call({"mode": "version"}).get("version", "unknown"))

    def categories(self) -> list[str]:
        return list(self._call({"mode": "get_cats"}).get("categories", []))

    def check(self, category: str) -> str:
        if not category:
            raise ClientError("no SABnzbd category set (config set sabnzbd.category NAME)")
        version = self.version()
        if category not in self.categories():
            raise ClientError(
                f"category {category!r} does not exist in SABnzbd; create it there first"
            )
        return version

    def add_nzb(self, nzb: bytes, name: str, category: str) -> str:
        if not category:
            raise ClientError("no SABnzbd category set")
        boundary = f"periodicool-{uuid.uuid4().hex}"
        filename = re.sub(r"[^\w.\- ]", "_", name)[:200] + ".nzb"
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="name"; filename="{filename}"\r\n'
                "Content-Type: application/x-nzb\r\n\r\n"
            ).encode()
            + nzb
            + f"\r\n--{boundary}--\r\n".encode()
        )
        data = self._call(
            {"mode": "addfile", "cat": category, "nzbname": name, "priority": -100},
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        ids = data.get("nzo_ids") or []
        if len(ids) != 1:
            raise ClientError(f"SABnzbd did not return one job ID: {ids}")
        return ids[0]

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        if not job_ids:
            return []
        wanted = set(job_ids)
        ids = ",".join(job_ids)
        out: dict[str, JobState] = {}
        queue = self._call({"mode": "queue", "cat": category, "category": category, "nzo_ids": ids})
        for slot in queue.get("queue", {}).get("slots", []):
            if slot.get("nzo_id") in wanted and slot.get("cat") == category:
                raw = str(slot.get("status", ""))
                status = JobStatus.DOWNLOADING if raw.lower() in _DOWNLOADING else JobStatus.QUEUED
                out[slot["nzo_id"]] = JobState(
                    job_id=slot["nzo_id"],
                    name=slot.get("filename", ""),
                    category=category,
                    status=status,
                    client_status=raw,
                    progress=_float(slot.get("percentage")),
                    size_bytes=_mb(slot.get("mb")),
                    size_left_bytes=_mb(slot.get("mbleft")),
                    time_left_s=_duration(slot.get("timeleft")),
                )
        history = self._call(
            {"mode": "history", "cat": category, "category": category, "nzo_ids": ids}
        )
        for slot in history.get("history", {}).get("slots", []):
            if slot.get("nzo_id") not in wanted or slot.get("category") != category:
                continue
            raw = str(slot.get("status", ""))
            if raw.lower() == "completed":
                status = JobStatus.COMPLETED
            elif raw.lower() == "failed":
                status = JobStatus.FAILED
            else:
                status = JobStatus.DOWNLOADING  # post-processing
            out[slot["nzo_id"]] = JobState(
                job_id=slot["nzo_id"],
                name=slot.get("name", ""),
                category=category,
                status=status,
                client_status=raw,
                progress=100.0,
                storage_path=slot.get("storage") or None,
                fail_message=slot.get("fail_message") or None,
                size_bytes=_int(slot.get("bytes")),
                size_left_bytes=0,
                in_history=True,
            )
        return list(out.values())

    def remove(self, job_id: str, category: str) -> bool:
        # Look the job up through the category-filtered calls first: if it is not ours,
        # it is not found, and nothing is deleted.
        found = [s for s in self.jobs([job_id], category) if s.job_id == job_id]
        if not found:
            return False
        if found[0].in_history:
            # History entry only; files of a finished job stay where they are.
            params = {"mode": "history", "name": "delete", "value": job_id, "del_files": 0}
        else:
            # Unfinished download: its partial files are useless, so SAB removes them.
            params = {"mode": "queue", "name": "delete", "value": job_id, "del_files": 1}
        self._call(params)
        return True


def _int(value) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _mb(value) -> int | None:
    try:
        return int(float(value) * 1024 * 1024)
    except TypeError, ValueError:
        return None


def _duration(value) -> int | None:
    """SAB's time left, "1:02:03" or "2:01:02:03" (days first), in seconds."""
    try:
        parts = [int(p) for p in str(value).split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 4:
        return None
    seconds = 0
    for part, unit in zip(reversed(parts), (1, 60, 3600, 86400), strict=False):
        seconds += part * unit
    return seconds


def _float(value) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return 0.0
