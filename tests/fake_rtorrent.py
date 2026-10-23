"""A fake rTorrent XML-RPC transport for tests. No network.

It wants basic auth, holds one torrent of another app (another label), and fails
the test on any call outside the ones PeriodiCool may make.
"""

import base64
import xmlrpc.client

from periodicool.bencode import info_hash, magnet_hash

RT_URL = "http://rt.test:8080/RPC2"
RT_USER, RT_PASS = "rt", "rt-pass-123"
FOREIGN_HASH = "f" * 40
ALLOWED = {
    "system.client_version",
    "system.multicall",
    "load.raw",
    "load.raw_start",
    "load.normal",
    "load.start",
    "d.erase",
    "d.custom1.set",
}


class FakeRtorrent:
    def __init__(self):
        self.calls = []
        self.erased = []
        self.torrents = {
            FOREIGN_HASH.upper(): {
                "d.name": "Movie",
                "d.custom1": "movies",
                "d.complete": 1,
                "d.state": 1,
                "d.left_bytes": 0,
                "d.size_bytes": 10,
                "d.base_path": "/data/movies/Movie",
                "d.directory": "/data/movies/Movie",
                "d.is_multi_file": 1,
            }
        }

    def __call__(self, method, url, headers, body, timeout):
        assert url == RT_URL and method == "POST"
        token = base64.b64encode(f"{RT_USER}:{RT_PASS}".encode()).decode()
        if headers.get("Authorization") != f"Basic {token}":
            return 401, b"Unauthorized", {}
        params, name = xmlrpc.client.loads(body)
        assert name in ALLOWED, name
        self.calls.append((name, params))

        def ok(result):
            return 200, xmlrpc.client.dumps((result,), methodresponse=True).encode(), {}

        if name == "system.client_version":
            return ok("0.9.8")
        if name == "system.multicall":
            answers = []
            for call in params[0]:
                job = call["params"][0]
                if job not in self.torrents:
                    answers.append({"faultCode": -501, "faultString": "Could not find info-hash."})
                else:
                    answers.append([self.torrents[job][call["methodName"]]])
            return ok(answers)
        if name.startswith("load."):
            source, commands = params[1], params[2:]
            if name.startswith("load.raw"):
                job = info_hash(source.data).upper()
            else:
                job = magnet_hash(source).upper()
            self.torrents[job] = {
                "d.name": "Linux 311",
                "d.custom1": "",
                "d.complete": 0,
                "d.state": 0 if name in ("load.raw", "load.normal") else 1,
                "d.left_bytes": 600,
                "d.size_bytes": 1000,
                "d.base_path": "",
                "d.directory": "/data/torrents",
                "d.is_multi_file": 0,
                "commands": list(commands),
            }
            for command in commands:
                if command.startswith("d.custom1.set="):
                    self.torrents[job]["d.custom1"] = command.split("=", 1)[1]
            return ok(0)
        if name == "d.custom1.set":
            self.torrents[params[0]]["d.custom1"] = params[1]
            return ok(0)
        if name == "d.erase":
            self.erased.append(params[0])
            self.torrents.pop(params[0], None)
            return ok(0)
        raise AssertionError(name)
