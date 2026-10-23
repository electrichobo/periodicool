"""A fake NZBGet JSON-RPC transport for tests. No network.

It holds one job of another app (another category), which must never come back
from the client, and fails the test if any call that changes settings is made.
"""

import base64
import itertools
import json

NZBGET_URL = "http://nzbget.test:6789"
NZBGET_USER = "nzbget"
NZBGET_PASS = "nzbget-pass-123"
FOREIGN_ID = 9001


class FakeNzbget:
    def __init__(self, categories=("movies", "mags")):
        self.categories = list(categories)
        self.calls = []
        self.added = []
        self.removed = []
        self._ids = itertools.count(1)
        self.queue = {
            FOREIGN_ID: {
                "NZBID": FOREIGN_ID,
                "NZBName": "Movie",
                "Category": "movies",
                "Status": "DOWNLOADING",
                "FileSizeMB": 100,
                "RemainingSizeMB": 50,
            }
        }
        self.history = {}

    def finish(self, job, status="SUCCESS/ALL", dest="/data/complete/mags/Linux 311"):
        g = self.queue.pop(int(job))
        self.history[int(job)] = {
            "NZBID": int(job),
            "Name": g["NZBName"],
            "Category": g["Category"],
            "Status": status,
            "DestDir": dest,
            "FileSizeMB": g["FileSizeMB"],
        }

    def __call__(self, method, url, headers, body, timeout):
        assert url == f"{NZBGET_URL}/jsonrpc" and method == "POST"
        request = json.loads(body)
        name, params = request["method"], request["params"]
        self.calls.append({"method": name, "params": params, "headers": headers})
        assert name not in ("saveconfig", "reload", "shutdown", "pausedownload", "resumedownload")
        expected = "Basic " + base64.b64encode(f"{NZBGET_USER}:{NZBGET_PASS}".encode()).decode()
        if headers.get("Authorization") != expected:
            return 401, b"Unauthorized"

        def ok(result):
            return 200, json.dumps(
                {"version": "1.1", "id": request["id"], "result": result}
            ).encode()

        if name == "version":
            return ok("24.3")
        if name == "config":
            items = [{"Name": "MainDir", "Value": "/data"}]
            items += [
                {"Name": f"Category{i + 1}.Name", "Value": c} for i, c in enumerate(self.categories)
            ]
            return ok(items)
        if name == "append":
            filename, content, category, priority, top, paused = params[:6]
            job = next(self._ids)
            self.added.append(
                {
                    "filename": filename,
                    "nzb": base64.b64decode(content),
                    "category": category,
                    "priority": priority,
                    "paused": paused,
                }
            )
            self.queue[job] = {
                "NZBID": job,
                "NZBName": filename[:-4],
                "Category": category,
                "Status": "PAUSED" if paused else "QUEUED",
                "FileSizeMB": 80,
                "RemainingSizeMB": 80,
            }
            return ok(job)
        if name == "listgroups":
            return ok(list(self.queue.values()))  # every job; the client must filter
        if name == "history":
            return ok(list(self.history.values()))
        if name == "editqueue":
            command, _, ids = params
            self.removed.append((command, ids))
            for job in ids:
                (self.history if command == "HistoryDelete" else self.queue).pop(job, None)
            return ok(True)
        return 200, json.dumps(
            {"id": request["id"], "error": {"code": -1, "message": f"unknown {name}"}}
        ).encode()
