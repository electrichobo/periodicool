"""NZBGet client (JSON-RPC, stdlib only).

NZBGet usually serves other apps too. This client only ever calls: `version`,
`config` (to see that our category exists; only the category names are read),
`append` (into our category), `listgroups` and `history` (answers filtered to our
NZBIDs and category), and `editqueue` to remove one of our own jobs. It never
changes NZBGet's settings.
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
from periodicool.clients.base import ClientError, JobState, JobStatus

log = logging.getLogger(__name__)

_QUEUED = {"queued", "paused"}


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


class NzbgetClient:
    name = "nzbget"
    # Set by sources.build_client from the download client list entry.
    row_id: int | None = None
    display_name = "NZBGet"
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
            raise ClientError("NZBGet address is required")
        self.url = url.rstrip("/")
        self._user, self._password = username or "", password or ""
        self._transport = transport or _urllib_transport
        self.timeout, self.min_interval = timeout, min_interval
        self._clock, self._sleep, self._last = clock, sleep, None
        self._ids = 0

    def redact(self, text) -> str:
        text = str(text)
        return text.replace(self._password, "REDACTED") if self._password else text

    def _call(self, method: str, *params):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._ids += 1
        body = json.dumps({"method": method, "params": list(params), "id": self._ids}).encode()
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if self._user or self._password:
            token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        try:
            status, raw = self._transport(
                "POST", f"{self.url}/jsonrpc", headers, body, self.timeout
            )[:2]
        except OSError as exc:
            raise ClientError(f"cannot reach NZBGet: {self.redact(exc)}") from None
        finally:
            self._last = self._clock()
        log.debug("NZBGet %s: %s", method, status)
        if status == 401:
            raise ClientError("NZBGet refused the username or password")
        if not 200 <= status < 300:
            raise ClientError(f"NZBGet returned {status}: {self.redact(raw[:200])}")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ClientError(f"bad JSON from NZBGet: {self.redact(raw[:200])}") from exc
        if data.get("error"):
            message = (
                data["error"].get("message") if isinstance(data["error"], dict) else data["error"]
            )
            raise ClientError(f"NZBGet error: {self.redact(message)}")
        return data.get("result")

    def version(self) -> str:
        return str(self._call("version"))

    def categories(self) -> list[str]:
        names = []
        for item in self._call("config") or []:
            if re.fullmatch(r"Category\d+\.Name", str(item.get("Name", ""))) and item.get("Value"):
                names.append(item["Value"])
        return names

    def check(self, category: str) -> str:
        if not category:
            raise ClientError("no NZBGet category set")
        version = self.version()
        if category not in self.categories():
            raise ClientError(
                f"category {category!r} does not exist in NZBGet; create it there first"
            )
        return version

    def add_nzb(
        self,
        nzb: bytes,
        name: str,
        category: str,
        priority: int | None = None,
        paused: bool = False,
    ) -> str:
        if not category:
            raise ClientError("no NZBGet category set")
        filename = re.sub(r"[^\w.\- ]", "_", name)[:200] + ".nzb"
        job = self._call(
            "append",
            filename,
            base64.b64encode(nzb).decode(),
            category,
            int(priority or 0),
            False,  # add to top
            bool(paused),
            "",  # dupe key
            0,  # dupe score
            "SCORE",  # dupe mode
            [],  # post-processing parameters
        )
        if not isinstance(job, int) or job <= 0:
            raise ClientError("NZBGet did not accept the NZB")
        return str(job)

    def jobs(self, job_ids: list[str], category: str) -> list[JobState]:
        if not job_ids:
            return []
        wanted = {str(j) for j in job_ids}
        out: dict[str, JobState] = {}
        for g in self._call("listgroups", 0) or []:
            job = str(g.get("NZBID"))
            if job not in wanted or g.get("Category") != category:
                continue
            raw = str(g.get("Status", ""))
            size = _mb(g.get("FileSizeMB"))
            left = _mb(g.get("RemainingSizeMB"))
            progress = 100.0 * (1 - left / size) if size and left is not None else 0.0
            out[job] = JobState(
                job_id=job,
                name=str(g.get("NZBName", "")),
                category=category,
                status=JobStatus.QUEUED if raw.lower() in _QUEUED else JobStatus.DOWNLOADING,
                client_status=raw,
                progress=progress,
                size_bytes=size,
                size_left_bytes=left,
            )
        for h in self._call("history", False) or []:
            job = str(h.get("NZBID"))
            if job not in wanted or job in out or h.get("Category") != category:
                continue
            raw = str(h.get("Status", ""))
            ok = raw.upper().startswith(("SUCCESS", "WARNING"))
            out[job] = JobState(
                job_id=job,
                name=str(h.get("Name", "")),
                category=category,
                status=JobStatus.COMPLETED if ok else JobStatus.FAILED,
                client_status=raw,
                progress=100.0,
                storage_path=h.get("DestDir") or None,
                fail_message=None if ok else raw,
                size_bytes=_mb(h.get("FileSizeMB")),
                size_left_bytes=0,
                in_history=True,
            )
        return list(out.values())

    def remove(self, job_id: str, category: str) -> bool:
        found = [s for s in self.jobs([job_id], category) if s.job_id == str(job_id)]
        if not found:
            return False
        # Queue: the unfinished download goes, with its partial files. History: the
        # entry goes; a finished download keeps its files.
        command = "HistoryDelete" if found[0].in_history else "GroupFinalDelete"
        if not self._call("editqueue", command, "", [int(job_id)]):
            raise ClientError(f"NZBGet did not remove job {job_id}")
        return True


def _mb(value) -> int | None:
    try:
        return int(float(value) * 1024 * 1024)
    except TypeError, ValueError:
        return None
