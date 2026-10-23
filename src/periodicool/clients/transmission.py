"""Transmission client (RPC, stdlib only).

Transmission has no categories, so PeriodiCool marks its own torrents with a label
(its category), as Sonarr does. This client only ever calls: `session-get` (the
version), `torrent-add` and `torrent-set` on the torrent it just added (label,
seed ratio, queue position), `torrent-get` for our own hashes (filtered again by
our label), and `torrent-remove` for one of our torrents, always keeping its
files. It never reads or changes Transmission's own settings beyond the version.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from periodicool import USER_AGENT
from periodicool.bencode import BencodeError, info_hash, magnet_hash
from periodicool.clients.base import ClientError, JobState, JobStatus

log = logging.getLogger(__name__)

_SESSION_HEADER = "X-Transmission-Session-Id"
# status: 0 stopped, 1 check wait, 2 checking, 3 download wait, 4 downloading,
# 5 seed wait, 6 seeding
_LOCAL_ERROR = 3


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers.items()) if exc.headers else {}


class TransmissionClient:
    name = "transmission"
    row_id: int | None = None
    display_name = "Transmission"
    category = ""
    path_map: list = []
    options: dict = {}

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        transport=None,
        timeout: float = 60.0,
        min_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not url:
            raise ClientError("Transmission address is required")
        self.url = url.rstrip("/")
        self._user, self._password = username or "", password or ""
        self._transport = transport or _urllib_transport
        self.timeout, self.min_interval = timeout, min_interval
        self._clock, self._sleep, self._last = clock, sleep, None
        self._session = ""

    def redact(self, text) -> str:
        text = str(text)
        return text.replace(self._password, "REDACTED") if self._password else text

    def _send(self, body: bytes):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if self._session:
            headers[_SESSION_HEADER] = self._session
        if self._user or self._password:
            token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        try:
            answer = self._transport("POST", f"{self.url}/rpc", headers, body, self.timeout)
        except OSError as exc:
            raise ClientError(f"cannot reach Transmission: {self.redact(exc)}") from None
        finally:
            self._last = self._clock()
        reply = {k.lower(): v for k, v in (answer[2] if len(answer) > 2 else {}).items()}
        return answer[0], answer[1], reply

    def _call(self, method: str, arguments: dict | None = None) -> dict:
        body = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        status, raw, reply = self._send(body)
        if status == 409:  # Transmission's CSRF guard: take the session id and ask again
            self._session = reply.get(_SESSION_HEADER.lower(), "")
            status, raw, reply = self._send(body)
        log.debug("Transmission %s: %s", method, status)
        if status == 401:
            raise ClientError("Transmission refused the username or password")
        if not 200 <= status < 300:
            raise ClientError(f"Transmission returned {status}: {self.redact(raw[:200])}")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ClientError(f"bad JSON from Transmission: {self.redact(raw[:200])}") from exc
        if data.get("result") != "success":
            raise ClientError(f"Transmission: {self.redact(data.get('result'))}")
        return data.get("arguments") or {}

    def version(self) -> str:
        return str(self._call("session-get", {"fields": ["version"]}).get("version", "unknown"))

    def check(self, category: str) -> str:
        if not category:
            raise ClientError("no label set; PeriodiCool marks its own torrents with it")
        return self.version()

    def add_nzb(self, nzb: bytes, name: str, category: str, **_) -> str:
        raise ClientError("Transmission takes torrents, not NZB files")

    def add_torrent(
        self,
        name: str,
        category: str,
        torrent: bytes | None = None,
        magnet: str | None = None,
        seed_ratio: float | None = None,
        seed_time_min: int | None = None,
        priority: str | None = None,
        paused: bool = False,
        directory: str | None = None,
    ) -> str:
        """Add a torrent with our label. Returns its info hash. Transmission has a seed
        ratio per torrent but no seeding time, so the time goal is left to it."""
        if not category:
            raise ClientError("no label set")
        try:
            job = info_hash(torrent) if torrent is not None else magnet_hash(magnet or "")
        except BencodeError as exc:
            raise ClientError(str(exc)) from exc
        args: dict = {"paused": bool(paused), "labels": [category]}
        if torrent is not None:
            args["metainfo"] = base64.b64encode(torrent).decode()
        else:
            args["filename"] = magnet
        if directory:
            args["download-dir"] = directory
        added = self._call("torrent-add", args)
        if "torrent-duplicate" in added and "torrent-added" not in added:
            raise ClientError("Transmission already has this torrent")
        # Older Transmission ignores labels on add; set them (and the ratio) on our torrent.
        extra: dict = {"ids": [job], "labels": [category]}
        if seed_ratio is not None:
            extra.update({"seedRatioLimit": float(seed_ratio), "seedRatioMode": 1})
        self._call("torrent-set", extra)
        if priority == "first":
            self._call("queue-move-top", {"ids": [job]})
        return job

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        if not job_ids:
            return []
        wanted = {j.lower() for j in job_ids}
        fields = [
            "hashString",
            "name",
            "status",
            "percentDone",
            "leftUntilDone",
            "totalSize",
            "eta",
            "downloadDir",
            "labels",
            "error",
            "errorString",
        ]
        found = self._call("torrent-get", {"ids": sorted(wanted), "fields": fields})
        out = []
        for t in found.get("torrents", []):
            job = str(t.get("hashString", "")).lower()
            if job not in wanted or category not in (t.get("labels") or []):
                continue
            code, done = int(t.get("status", 0)), float(t.get("percentDone") or 0)
            if int(t.get("error") or 0) == _LOCAL_ERROR:
                status = JobStatus.FAILED
            elif done >= 1 or code in (5, 6):
                status = JobStatus.COMPLETED
            elif code in (0, 3):
                status = JobStatus.QUEUED
            else:
                status = JobStatus.DOWNLOADING
            eta = t.get("eta")
            folder, name = str(t.get("downloadDir") or "").rstrip("/"), str(t.get("name", ""))
            out.append(
                JobState(
                    job_id=job,
                    name=name,
                    category=category,
                    status=status,
                    client_status=str(code),
                    progress=done * 100,
                    storage_path=f"{folder}/{name}" if folder else None,
                    fail_message=(t.get("errorString") or None)
                    if status is JobStatus.FAILED
                    else None,
                    size_bytes=t.get("totalSize"),
                    size_left_bytes=t.get("leftUntilDone"),
                    time_left_s=eta if isinstance(eta, int) and eta >= 0 else None,
                    in_history=status is JobStatus.COMPLETED,
                )
            )
        return out

    def remove(self, job_id: str, category: str) -> bool:
        """Remove one of our torrents from Transmission. Its files always stay."""
        if not [s for s in self.jobs([job_id], category) if s.job_id == job_id.lower()]:
            return False
        self._call("torrent-remove", {"ids": [job_id.lower()], "delete-local-data": False})
        return True
