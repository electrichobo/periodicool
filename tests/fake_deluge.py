"""A fake Deluge Web UI JSON-RPC transport for tests. No network.

It wants a login cookie, starts disconnected from its daemon, holds one torrent of
another app (another label), and fails the test on any call that changes Deluge's
own settings.
"""

import base64
import json

from periodicool.bencode import info_hash, magnet_hash

DE_URL = "http://deluge.test:8112"
DE_PASS = "deluge-pass-123"
FOREIGN_HASH = "d" * 40


class FakeDeluge:
    def __init__(self, labels=("movies", "mags"), label_plugin=True):
        self.labels = list(labels)
        self.label_plugin = label_plugin
        self.connected = False
        self.calls = []
        self.removed = []
        self.options = {}
        self.top = []
        self.torrents = {
            FOREIGN_HASH: {
                "name": "Movie",
                "state": "Seeding",
                "progress": 100.0,
                "label": "movies",
                "save_path": "/data/movies",
            }
        }

    def set_state(self, job, state, progress, message=""):
        self.torrents[job].update(state=state, progress=progress, message=message)

    def __call__(self, method, url, headers, body, timeout):
        assert url == f"{DE_URL}/json" and method == "POST"
        request = json.loads(body)
        name, params = request["method"], request["params"]
        self.calls.append(name)
        assert not name.startswith(("core.set_config", "core.set_torrent_options_all")), name
        cookie = {}

        def ok(result):
            return (
                200,
                json.dumps({"id": request["id"], "result": result, "error": None}).encode(),
                cookie,
            )

        def error(message):
            return (
                200,
                json.dumps(
                    {"id": request["id"], "result": None, "error": {"message": message, "code": 2}}
                ).encode(),
                {},
            )

        if name == "auth.login":
            if params != [DE_PASS]:
                return ok(False)
            cookie = {"Set-Cookie": "_session_id=s3ss10n; Path=/json"}
            return ok(True)
        if headers.get("Cookie") != "_session_id=s3ss10n":
            return error("Not authenticated")
        if name == "web.connected":
            return ok(self.connected)
        if name == "web.get_hosts":
            return ok([["host1", "127.0.0.1", 58846, "Online"]])
        if name == "web.connect":
            self.connected = params == ["host1"]
            return ok(None)
        assert self.connected, f"{name} before connecting to the daemon"
        if name == "daemon.info":
            return ok("2.1.1")
        if name.startswith("label."):
            if not self.label_plugin:
                return error(f"Unknown method {name}")
            if name == "label.get_labels":
                return ok(self.labels)
            if name == "label.set_torrent":
                job, label = params
                self.torrents[job]["label"] = label
                return ok(None)
        if name in ("core.add_torrent_file", "core.add_torrent_magnet"):
            if name == "core.add_torrent_file":
                job = info_hash(base64.b64decode(params[1]))
            else:
                job = magnet_hash(params[0])
            self.torrents[job] = {
                "name": "Linux 311",
                "state": "Paused" if params[-1].get("add_paused") else "Downloading",
                "progress": 0.0,
                "label": "",
                "save_path": params[-1].get("download_location", "/data/torrents"),
                "total_wanted": 1000,
                "total_done": 0,
                "eta": 0,
                "added_options": params[-1],
            }
            return ok(job)
        if name == "core.set_torrent_options":
            for job in params[0]:
                self.options[job] = params[1]
            return ok(None)
        if name == "core.queue_top":
            self.top += params[0]
            return ok(None)
        if name == "core.get_torrents_status":
            # Answers whatever ids it is asked for; the client filters by label.
            wanted = set(params[0]["id"]) | {FOREIGN_HASH}
            return ok({h: t for h, t in self.torrents.items() if h in wanted})
        if name == "core.remove_torrent":
            self.removed.append(tuple(params))
            self.torrents.pop(params[0], None)
            return ok(True)
        return error(f"Unknown method {name}")
