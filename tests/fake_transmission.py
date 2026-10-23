"""A fake Transmission RPC transport for tests. No network.

It insists on the session-id handshake, holds one torrent of another app (no
label of ours), and fails the test on any call that changes Transmission's own
settings.
"""

import base64
import json

from periodicool.bencode import info_hash, magnet_hash

TR_URL = "http://tr.test:9091/transmission"
TR_USER = "admin"
TR_PASS = "tr-pass-123"
SESSION = "abc-session"
FOREIGN_HASH = "e" * 40


class FakeTransmission:
    def __init__(self):
        self.calls = []
        self.torrents = {
            FOREIGN_HASH: {
                "hashString": FOREIGN_HASH,
                "name": "Some Movie",
                "status": 6,
                "percentDone": 1.0,
                "labels": ["movies"],
                "downloadDir": "/data/movies",
                "error": 0,
            }
        }
        self.removed = []
        self.moved_top = []

    def set_state(self, job, status, done, error=0, error_string=""):
        self.torrents[job].update(
            status=status, percentDone=done, error=error, errorString=error_string
        )

    def __call__(self, method, url, headers, body, timeout):
        assert url == f"{TR_URL}/rpc" and method == "POST"
        expected = "Basic " + base64.b64encode(f"{TR_USER}:{TR_PASS}".encode()).decode()
        if headers.get("Authorization") != expected:
            return 401, b"Unauthorized", {}
        if headers.get("X-Transmission-Session-Id") != SESSION:
            return 409, b"", {"X-Transmission-Session-Id": SESSION}
        request = json.loads(body)
        name, args = request["method"], request.get("arguments", {})
        self.calls.append({"method": name, "arguments": args})
        assert name not in ("session-set", "blocklist-update", "port-test"), name

        def ok(arguments=None):
            return 200, json.dumps({"result": "success", "arguments": arguments or {}}).encode(), {}

        if name == "session-get":
            return ok({"version": "4.0.5 (a6fe2a64aa)"})
        if name == "torrent-add":
            job = (
                info_hash(base64.b64decode(args["metainfo"]))
                if "metainfo" in args
                else magnet_hash(args["filename"])
            )
            self.torrents[job] = {
                "hashString": job,
                "name": "Linux 311",
                "status": 0 if args.get("paused") else 4,
                "percentDone": 0.0,
                "labels": [],  # like Transmission 3: labels come from torrent-set
                "downloadDir": args.get("download-dir", "/data/torrents"),
                "totalSize": 1000,
                "leftUntilDone": 1000,
                "eta": -1,
                "error": 0,
                "added": args,
            }
            return ok({"torrent-added": {"hashString": job, "id": 1, "name": "Linux 311"}})
        if name == "torrent-set":
            for job in args["ids"]:
                self.torrents[job]["labels"] = args.get("labels", self.torrents[job]["labels"])
                self.torrents[job]["set"] = args
            return ok()
        if name == "queue-move-top":
            self.moved_top += args["ids"]
            return ok()
        if name == "torrent-get":
            # Like Transmission: whatever ids it is asked for, so the client must filter.
            wanted = set(args["ids"]) | {FOREIGN_HASH}
            return ok({"torrents": [t for h, t in self.torrents.items() if h in wanted]})
        if name == "torrent-remove":
            self.removed.append((args["ids"], args["delete-local-data"]))
            for job in args["ids"]:
                self.torrents.pop(job, None)
            return ok()
        return 200, json.dumps({"result": f"method name not recognized: {name}"}).encode(), {}
