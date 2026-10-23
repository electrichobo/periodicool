"""rTorrent client (XML-RPC, stdlib only).

PeriodiCool's category is rTorrent's label (`d.custom1`, what ruTorrent shows as
the label). This client only ever calls: `system.client_version`, a load command
that adds a torrent with our label (and directory and priority when set), reads of
our own hashes (filtered again by our label), `d.custom1.set` on our torrent after
import, and `d.erase` on one of our torrents, which leaves its files alone. It
never changes rTorrent's own settings.
"""

from __future__ import annotations

import base64
import logging
import time
import urllib.error
import urllib.request
import xmlrpc.client
from collections.abc import Callable

from periodicool import USER_AGENT
from periodicool.bencode import BencodeError, info_hash, magnet_hash
from periodicool.clients.base import ClientError, JobState, JobStatus

log = logging.getLogger(__name__)

_FIELDS = (
    "d.name",
    "d.custom1",
    "d.complete",
    "d.state",
    "d.left_bytes",
    "d.size_bytes",
    "d.base_path",
    "d.directory",
    "d.is_multi_file",
)
_PRIORITIES = {"0", "1", "2", "3"}


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers.items()) if exc.headers else {}


def _clean(value: str) -> str:
    """Keep a value from breaking out of an rTorrent command (`key=value`)."""
    return str(value).replace('"', "").replace(";", "").replace("\n", " ")


class RtorrentClient:
    name = "rtorrent"
    row_id: int | None = None
    display_name = "rTorrent"
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
            raise ClientError("rTorrent address is required")
        self.url = url
        self._user, self._password = username or "", password or ""
        self._transport = transport or _urllib_transport
        self.timeout, self.min_interval = timeout, min_interval
        self._clock, self._sleep, self._last = clock, sleep, None

    def redact(self, text) -> str:
        text = str(text)
        return text.replace(self._password, "REDACTED") if self._password else text

    def _call(self, method: str, *params):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        body = xmlrpc.client.dumps(tuple(params), method, allow_none=False).encode()
        headers = {"User-Agent": USER_AGENT, "Content-Type": "text/xml"}
        if self._user or self._password:
            token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        try:
            answer = self._transport("POST", self.url, headers, body, self.timeout)
        except OSError as exc:
            raise ClientError(f"cannot reach rTorrent: {self.redact(exc)}") from None
        finally:
            self._last = self._clock()
        status, raw = answer[0], answer[1]
        log.debug("rTorrent %s: %s", method, status)
        if status == 401:
            raise ClientError("rTorrent refused the username or password")
        if not 200 <= status < 300:
            raise ClientError(f"rTorrent returned {status}: {self.redact(raw[:200])}")
        try:
            (result,), _ = xmlrpc.client.loads(raw)
        except xmlrpc.client.Fault as exc:
            raise ClientError(f"rTorrent error: {self.redact(exc.faultString)}") from None
        except Exception as exc:  # noqa: BLE001 (expat raises several kinds)
            raise ClientError(f"bad answer from rTorrent: {self.redact(raw[:200])}") from exc
        return result

    def version(self) -> str:
        return str(self._call("system.client_version"))

    def check(self, category: str) -> str:
        if not category:
            raise ClientError("no label set; PeriodiCool marks its own torrents with it")
        return self.version()

    def add_nzb(self, nzb: bytes, name: str, category: str, **_) -> str:
        raise ClientError("rTorrent takes torrents, not NZB files")

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
        """Add a torrent with our label. Returns its info hash. rTorrent's seed goals
        are ratio groups in its own settings, so they are left to it."""
        if not category:
            raise ClientError("no label set")
        try:
            job = info_hash(torrent) if torrent is not None else magnet_hash(magnet or "")
        except BencodeError as exc:
            raise ClientError(str(exc)) from exc
        commands = [f"d.custom1.set={_clean(category)}"]
        if directory:
            commands.append(f'd.directory.set="{_clean(directory)}"')
        if priority in _PRIORITIES:
            commands.append(f"d.priority.set={priority}")
        if torrent is not None:
            method = "load.raw" if paused else "load.raw_start"
            self._call(method, "", xmlrpc.client.Binary(torrent), *commands)
        else:
            method = "load.normal" if paused else "load.start"
            self._call(method, "", magnet, *commands)
        return job

    def _status(self, job: str) -> dict | None:
        """One torrent's fields, or None when rTorrent does not have it."""
        calls = [{"methodName": f, "params": [job.upper()]} for f in _FIELDS]
        answers = self._call("system.multicall", calls)
        if any(isinstance(a, dict) for a in answers):  # a fault: unknown hash
            return None
        return {f: a[0] for f, a in zip(_FIELDS, answers, strict=True)}

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        out = []
        for job in sorted({j.lower() for j in job_ids}):
            t = self._status(job)
            if t is None or str(t["d.custom1"]) != category:
                continue
            if t["d.complete"]:
                status = JobStatus.COMPLETED
            elif not t["d.state"]:
                status = JobStatus.QUEUED
            else:
                status = JobStatus.DOWNLOADING
            name, size, left = str(t["d.name"]), t["d.size_bytes"], t["d.left_bytes"]
            path = str(t["d.base_path"] or "")
            if not path and t["d.directory"]:
                folder = str(t["d.directory"]).rstrip("/")
                path = folder if t["d.is_multi_file"] else f"{folder}/{name}"
            out.append(
                JobState(
                    job_id=job,
                    name=name,
                    category=category,
                    status=status,
                    client_status="complete"
                    if t["d.complete"]
                    else ("started" if t["d.state"] else "stopped"),
                    progress=100.0 if not size else (size - left) * 100 / size,
                    storage_path=path or None,
                    fail_message=None,
                    size_bytes=size,
                    size_left_bytes=left,
                    time_left_s=None,
                    in_history=status is JobStatus.COMPLETED,
                )
            )
        return out

    def remove(self, job_id: str, category: str) -> bool:
        """Remove one of our torrents from rTorrent. `d.erase` never deletes files."""
        if not self.jobs([job_id], category):
            return False
        self._call("d.erase", job_id.upper())
        return True
