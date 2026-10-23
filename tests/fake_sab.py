"""A fake SABnzbd for tests. Ignores filters on purpose so the client's own
filtering is tested: every response also carries other apps' jobs."""

import itertools
import json
import urllib.parse

SAB_KEY = "sab-secret-key-456"
SAB_URL = "http://sab.test:8080"

FOREIGN_QUEUE = {
    "nzo_id": "SABnzbd_nzo_movie",
    "filename": "Some.Movie.2026",
    "cat": "movies",
    "status": "Downloading",
    "percentage": "40",
    "mb": "4000",
    "mbleft": "2400",
    "timeleft": "0:10:00",
}
FOREIGN_HISTORY = {
    "nzo_id": "SABnzbd_nzo_tv",
    "name": "Some.Show.S01E01",
    "category": "tv",
    "status": "Completed",
    "storage": "/data/complete/tv/Some.Show",
}


def make_nzb(*names: str) -> bytes:
    files = "".join(
        f'<file poster="p" date="1" subject="[1/1] - &quot;{n}&quot; yEnc (1/1)">'
        f"<groups><group>alt.binaries.test</group></groups>"
        f'<segments><segment bytes="10" number="1">x@y</segment></segments></file>'
        for n in names
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{files}</nzb>'
    ).encode()


class FakeSab:
    def __init__(self, categories=("movies", "tv", "mags")):
        self.categories = list(categories)
        self.calls = []
        self.added = []  # (category, nzbname, body)
        self.queue = {}  # nzo_id -> slot
        self.history = {}
        self.deleted = []  # (mode, nzo_id, del_files)
        self.priorities = []  # priority sent with each addfile
        self._ids = itertools.count(1)

    def complete(self, job_id, storage, status="Completed", fail_message=""):
        slot = self.queue.pop(job_id)
        self.history[job_id] = {
            "bytes": int(float(slot.get("mb", 0)) * 1024 * 1024),
            "nzo_id": job_id,
            "name": slot["filename"],
            "category": slot["cat"],
            "status": status,
            "storage": storage,
            "fail_message": fail_message,
        }

    def __call__(self, method, url, headers, body, timeout):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        self.calls.append(
            {"method": method, "url": url, "query": q, "headers": headers, "body": body}
        )
        mode = q.get("mode")
        if q.get("apikey") != SAB_KEY:
            return 200, json.dumps({"status": False, "error": "API Key Incorrect"}).encode()
        if mode == "version":
            return 200, b'{"version": "5.1.3"}'
        if mode == "get_cats":
            return 200, json.dumps({"categories": ["*", *self.categories]}).encode()
        if mode == "addfile":
            assert method == "POST" and "multipart/form-data" in headers["Content-Type"]
            job = f"SABnzbd_nzo_{next(self._ids)}"
            self.added.append((q["cat"], q["nzbname"], body))
            self.priorities.append(q.get("priority"))
            self.queue[job] = {
                "nzo_id": job,
                "filename": q["nzbname"],
                "cat": q["cat"],
                "status": "Queued",
                "percentage": "0",
                "mb": "80.5",
                "mbleft": "80.5",
                "timeleft": "0:00:00",
            }
            return 200, json.dumps({"status": True, "nzo_ids": [job]}).encode()
        if mode in ("queue", "history") and q.get("name") == "delete":
            # Mirrors SAB: deletes whatever ID it is given, so the client must check first.
            self.deleted.append((mode, q["value"], q.get("del_files")))
            (self.queue if mode == "queue" else self.history).pop(q["value"], None)
            return 200, json.dumps({"status": True, "nzo_ids": [q["value"]]}).encode()
        if mode == "queue":
            slots = [*self.queue.values(), FOREIGN_QUEUE]
            return 200, json.dumps({"queue": {"slots": slots}}).encode()
        if mode == "history":
            slots = [*self.history.values(), FOREIGN_HISTORY]
            return 200, json.dumps({"history": {"slots": slots}}).encode()
        return 200, json.dumps({"status": False, "error": f"unexpected mode {mode}"}).encode()
