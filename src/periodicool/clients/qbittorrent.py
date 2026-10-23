"""qBittorrent client (Web API v2, stdlib only).

qBittorrent usually serves other apps too. This client only ever calls: login,
`app/version` and `torrents/categories` (read), `torrents/add` (into our category,
with the seed goals for that torrent), `torrents/info` filtered to our category
and our hashes, and `torrents/delete` for one of our hashes, always keeping its
files. It never reads or changes qBittorrent's preferences or global limits.
Answers are filtered again here, so a torrent that is not ours is never returned
or removed.
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
from periodicool.bencode import BencodeError, info_hash, magnet_hash
from periodicool.clients.base import ClientError, JobState, JobStatus

log = logging.getLogger(__name__)

# (method, url, headers, body, timeout) -> (status, body, response headers)
Transport = Callable[[str, str, dict, bytes | None, float], tuple]

_DOWNLOADING = {
    "downloading",
    "metadl",
    "forcedmetadl",
    "stalleddl",
    "queueddl",
    "checkingdl",
    "forceddl",
    "allocating",
    "moving",
    "checkingresumedata",
}
_QUEUED = {"pauseddl", "stoppeddl"}
_FAILED = {"error", "missingfiles"}
_NO_ETA = 8_640_000  # qBittorrent's "infinity"


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers.items()) if exc.headers else {}


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = f"periodicool-{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += str(value).encode() + b"\r\n"
    for key, (filename, content, ctype) in files.items():
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
        ).encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class QbittorrentClient:
    name = "qbittorrent"
    # Set by sources.build_client from the download client list entry.
    row_id: int | None = None
    display_name = "qBittorrent"
    category = ""
    path_map: list = []

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        transport: Transport | None = None,
        timeout: float = 60.0,
        min_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not url:
            raise ClientError("qBittorrent address is required")
        self.url = url.rstrip("/")
        self._user, self._password = username or "", password or ""
        self._transport = transport or _urllib_transport
        self.timeout, self.min_interval = timeout, min_interval
        self._clock, self._sleep, self._last = clock, sleep, None
        self._sid: str | None = None

    def redact(self, text) -> str:
        text = str(text)
        if self._password:
            text = text.replace(self._password, "REDACTED")
        return re.sub(r"(SID=)[^;\s]+", r"\1REDACTED", text)

    def _send(self, method: str, path: str, body: bytes | None, content_type: str | None):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        # qBittorrent checks Referer/Origin against its own address (CSRF protection).
        headers = {"User-Agent": USER_AGENT, "Referer": self.url, "Origin": self.url}
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            answer = self._transport(method, f"{self.url}{path}", headers, body, self.timeout)
        except OSError as exc:
            raise ClientError(f"cannot reach qBittorrent: {self.redact(exc)}") from None
        finally:
            self._last = self._clock()
        status, raw = answer[0], answer[1]
        response_headers = answer[2] if len(answer) > 2 else {}
        log.debug("qBittorrent %s %s: %s", method, path.split("?")[0], status)
        return status, raw, {k.lower(): v for k, v in response_headers.items()}

    def login(self) -> None:
        body = urllib.parse.urlencode({"username": self._user, "password": self._password})
        status, raw, headers = self._send(
            "POST", "/api/v2/auth/login", body.encode(), "application/x-www-form-urlencoded"
        )
        if status == 403:
            raise ClientError("qBittorrent refused this address (too many failed logins)")
        if status != 200 or raw.strip() != b"Ok.":
            raise ClientError("qBittorrent refused the username or password")
        if m := re.search(r"SID=([^;]+)", headers.get("set-cookie", "")):
            self._sid = m.group(1)

    def _request(self, method: str, path: str, form: dict | None = None, multipart=None) -> bytes:
        body, ctype = None, None
        if multipart is not None:
            body, ctype = multipart
        elif form is not None:
            body, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        if self._sid is None:
            self.login()
        status, raw, _ = self._send(method, path, body, ctype)
        if status == 403:  # session expired: log in again once
            self.login()
            status, raw, _ = self._send(method, path, body, ctype)
        if not 200 <= status < 300:
            raise ClientError(f"qBittorrent returned {status}: {self.redact(raw[:200])}")
        return raw

    def _json(self, path: str):
        raw = self._request("GET", path)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ClientError(f"bad JSON from qBittorrent: {self.redact(raw[:200])}") from exc

    def version(self) -> str:
        return self._request("GET", "/api/v2/app/version").decode("utf-8", "replace").strip()

    def categories(self) -> list[str]:
        data = self._json("/api/v2/torrents/categories")
        return list(data) if isinstance(data, dict) else []

    def check(self, category: str) -> str:
        if not category:
            raise ClientError("no qBittorrent category set")
        version = self.version()
        if category not in self.categories():
            raise ClientError(
                f"category {category!r} does not exist in qBittorrent; create it there first"
            )
        return version

    def add_nzb(self, nzb: bytes, name: str, category: str) -> str:
        raise ClientError("qBittorrent takes torrents, not NZB files")

    def add_torrent(
        self,
        name: str,
        category: str,
        torrent: bytes | None = None,
        magnet: str | None = None,
        seed_ratio: float | None = None,
        seed_time_min: int | None = None,
    ) -> str:
        """Add a torrent file or magnet in our category. Returns its info hash, which is
        how we find it again. Seed goals apply to this torrent only."""
        if not category:
            raise ClientError("no qBittorrent category set")
        try:
            job = info_hash(torrent) if torrent is not None else magnet_hash(magnet or "")
        except BencodeError as exc:
            raise ClientError(str(exc)) from exc
        fields: dict = {"category": category, "paused": "false", "stopped": "false"}
        if seed_ratio is not None:
            fields["ratioLimit"] = f"{seed_ratio:g}"
        if seed_time_min is not None:
            fields["seedingTimeLimit"] = int(seed_time_min)
        files = {}
        if torrent is not None:
            filename = re.sub(r"[^\w.\- ]", "_", name)[:200] + ".torrent"
            files["torrents"] = (filename, torrent, "application/x-bittorrent")
        else:
            fields["urls"] = magnet
        raw = self._request("POST", "/api/v2/torrents/add", multipart=_multipart(fields, files))
        if raw.strip() != b"Ok.":
            raise ClientError(f"qBittorrent did not add the torrent: {self.redact(raw[:200])}")
        return job

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        if not job_ids:
            return []
        wanted = {j.lower() for j in job_ids}
        query = urllib.parse.urlencode({"category": category, "hashes": "|".join(sorted(wanted))})
        out = []
        for t in self._json(f"/api/v2/torrents/info?{query}") or []:
            job = str(t.get("hash", "")).lower()
            if job not in wanted or t.get("category") != category:
                continue
            raw = str(t.get("state", ""))
            state = raw.lower()
            progress = float(t.get("progress") or 0) * 100
            if state in _FAILED:
                status = JobStatus.FAILED
            elif state.endswith("up") or progress >= 100:
                status = JobStatus.COMPLETED
            elif state in _QUEUED:
                status = JobStatus.QUEUED
            else:
                status = JobStatus.DOWNLOADING
            eta = t.get("eta")
            out.append(
                JobState(
                    job_id=job,
                    name=str(t.get("name", "")),
                    category=category,
                    status=status,
                    client_status=raw,
                    progress=progress,
                    storage_path=t.get("content_path") or None,
                    fail_message=raw if status is JobStatus.FAILED else None,
                    size_bytes=t.get("size"),
                    size_left_bytes=t.get("amount_left"),
                    time_left_s=eta if isinstance(eta, int) and 0 <= eta < _NO_ETA else None,
                    in_history=status is JobStatus.COMPLETED,
                )
            )
        return out

    def remove(self, job_id: str, category: str) -> bool:
        """Remove one of our torrents from qBittorrent. Its files always stay."""
        if not [s for s in self.jobs([job_id], category) if s.job_id == job_id.lower()]:
            return False
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            form={"hashes": job_id.lower(), "deleteFiles": "false"},
        )
        return True
