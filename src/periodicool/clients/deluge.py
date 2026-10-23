"""Deluge client (Web UI JSON-RPC, stdlib only).

PeriodiCool's category is a Deluge label, so the Label plugin must be on. This
client only ever calls: login, connect the Web UI to its daemon when it is not,
the daemon's version and the label list (read), add a torrent file or magnet and
then set our label, seed ratio and queue position on that torrent, read our own
hashes (filtered again by our label), and remove one of our torrents, always
keeping its files. It never changes Deluge's own settings.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from periodicool import USER_AGENT
from periodicool.bencode import BencodeError, info_hash, magnet_hash
from periodicool.clients.base import ClientError, JobState, JobStatus

log = logging.getLogger(__name__)

_FIELDS = [
    "name",
    "state",
    "progress",
    "total_wanted",
    "total_done",
    "eta",
    "save_path",
    "label",
    "message",
]


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers.items()) if exc.headers else {}


class DelugeClient:
    name = "deluge"
    row_id: int | None = None
    display_name = "Deluge"
    category = ""
    path_map: list = []
    options: dict = {}

    def __init__(
        self,
        url: str,
        password: str,
        transport=None,
        timeout: float = 60.0,
        min_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not url:
            raise ClientError("Deluge address is required")
        self.url = url.rstrip("/")
        self._password = password or ""
        self._transport = transport or _urllib_transport
        self.timeout, self.min_interval = timeout, min_interval
        self._clock, self._sleep, self._last = clock, sleep, None
        self._cookie = ""
        self._ids = 0
        self._ready = False

    def redact(self, text) -> str:
        text = str(text)
        if self._password:
            text = text.replace(self._password, "REDACTED")
        return re.sub(r"(_session_id=)[^;\s]+", r"\1REDACTED", text)

    def _raw(self, method: str, *params):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._ids += 1
        body = json.dumps({"method": method, "params": list(params), "id": self._ids}).encode()
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            answer = self._transport("POST", f"{self.url}/json", headers, body, self.timeout)
        except OSError as exc:
            raise ClientError(f"cannot reach Deluge: {self.redact(exc)}") from None
        finally:
            self._last = self._clock()
        status, raw = answer[0], answer[1]
        reply = {k.lower(): v for k, v in (answer[2] if len(answer) > 2 else {}).items()}
        if m := re.search(r"(_session_id=[^;]+)", reply.get("set-cookie", "")):
            self._cookie = m.group(1)
        log.debug("Deluge %s: %s", method, status)
        if not 200 <= status < 300:
            raise ClientError(f"Deluge returned {status}: {self.redact(raw[:200])}")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ClientError(f"bad JSON from Deluge: {self.redact(raw[:200])}") from exc
        if data.get("error"):
            message = (
                data["error"].get("message") if isinstance(data["error"], dict) else data["error"]
            )
            raise ClientError(f"Deluge error: {self.redact(message)}")
        return data.get("result")

    def _ensure(self) -> None:
        """Log in, and connect the Web UI to its daemon when it is not connected."""
        if self._ready:
            return
        if not self._raw("auth.login", self._password):
            raise ClientError("Deluge refused the password")
        if not self._raw("web.connected"):
            hosts = self._raw("web.get_hosts") or []
            if not hosts:
                raise ClientError("Deluge's Web UI has no daemon to connect to")
            self._raw("web.connect", hosts[0][0])
            if not self._raw("web.connected"):
                raise ClientError("Deluge's Web UI could not connect to its daemon")
        self._ready = True

    def _call(self, method: str, *params):
        self._ensure()
        return self._raw(method, *params)

    def version(self) -> str:
        return str(self._call("daemon.info"))

    def labels(self) -> list[str]:
        try:
            return list(self._call("label.get_labels") or [])
        except ClientError as exc:
            raise ClientError(
                "Deluge's Label plugin is off; turn it on (Preferences, Plugins) so "
                "PeriodiCool can mark its own torrents"
            ) from exc

    def check(self, category: str) -> str:
        if not category:
            raise ClientError("no label set")
        version = self.version()
        if category.lower() not in [label.lower() for label in self.labels()]:
            raise ClientError(f"label {category!r} does not exist in Deluge; create it there first")
        return version

    def add_nzb(self, nzb: bytes, name: str, category: str, **_) -> str:
        raise ClientError("Deluge takes torrents, not NZB files")

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
        **_,
    ) -> str:
        """Add a torrent, then set our label (Deluge labels are lower case) and the seed
        ratio on it. Deluge has no seeding-time limit per torrent."""
        if not category:
            raise ClientError("no label set")
        try:
            job = info_hash(torrent) if torrent is not None else magnet_hash(magnet or "")
        except BencodeError as exc:
            raise ClientError(str(exc)) from exc
        options: dict = {"add_paused": bool(paused)}
        if directory:
            options["download_location"] = directory
        if torrent is not None:
            filename = re.sub(r"[^\w.\- ]", "_", name)[:200] + ".torrent"
            added = self._call(
                "core.add_torrent_file", filename, base64.b64encode(torrent).decode(), options
            )
        else:
            added = self._call("core.add_torrent_magnet", magnet, options)
        if not added:
            raise ClientError("Deluge did not add the torrent (it may already have it)")
        self._call("label.set_torrent", job, category.lower())
        if seed_ratio is not None:
            self._call(
                "core.set_torrent_options",
                [job],
                {"stop_at_ratio": True, "stop_ratio": float(seed_ratio)},
            )
        if priority == "first":
            self._call("core.queue_top", [job])
        return job

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        if not job_ids:
            return []
        wanted = {j.lower() for j in job_ids}
        found = self._call("core.get_torrents_status", {"id": sorted(wanted)}, _FIELDS) or {}
        out = []
        for job, t in found.items():
            job = job.lower()
            if job not in wanted or str(t.get("label", "")).lower() != category.lower():
                continue
            state = str(t.get("state", ""))
            progress = float(t.get("progress") or 0)
            if state == "Error":
                status = JobStatus.FAILED
            elif state == "Seeding" or progress >= 100:
                status = JobStatus.COMPLETED
            elif state in ("Paused", "Queued"):
                status = JobStatus.QUEUED
            else:
                status = JobStatus.DOWNLOADING
            folder, name = str(t.get("save_path") or "").rstrip("/"), str(t.get("name", ""))
            wanted_bytes, done = t.get("total_wanted"), t.get("total_done")
            eta = t.get("eta")
            out.append(
                JobState(
                    job_id=job,
                    name=name,
                    category=category,
                    status=status,
                    client_status=state,
                    progress=progress,
                    storage_path=f"{folder}/{name}" if folder else None,
                    fail_message=(t.get("message") or state)
                    if status is JobStatus.FAILED
                    else None,
                    size_bytes=wanted_bytes,
                    size_left_bytes=(wanted_bytes - done)
                    if wanted_bytes is not None and done is not None
                    else None,
                    time_left_s=int(eta) if isinstance(eta, int | float) and eta > 0 else None,
                    in_history=status is JobStatus.COMPLETED,
                )
            )
        return out

    def set_category(self, job_id: str, category: str, new: str) -> bool:
        """Move one of our torrents to another label (post-import category)."""
        if not [s for s in self.jobs([job_id], category) if s.job_id == job_id.lower()]:
            return False
        self._call("label.set_torrent", job_id.lower(), new.lower())
        return True

    def remove(self, job_id: str, category: str) -> bool:
        """Remove one of our torrents from Deluge. Its files always stay."""
        if not [s for s in self.jobs([job_id], category) if s.job_id == job_id.lower()]:
            return False
        self._call("core.remove_torrent", job_id.lower(), False)
        return True
