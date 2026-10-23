"""A fake qBittorrent Web API transport for tests. No network.

It holds one torrent of another app (another category), which must never come back
from the client, and it fails the test if any preferences or global endpoint is
called.
"""

import json
import urllib.parse

from periodicool.bencode import info_hash, magnet_hash

QB_URL = "http://qbit.test:8080"
QB_USER = "admin"
QB_PASS = "qbit-password-123"
FOREIGN_HASH = "f" * 40


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(value)


def make_torrent(name="Linux Magazine 311", files=("Linux Magazine 311.pdf",)) -> bytes:
    info = {
        "name": name,
        "piece length": 16384,
        "pieces": b"\x00" * 20,
        "files": [{"length": 1000, "path": [f]} for f in files],
    }
    return bencode({"announce": "http://tracker.test/announce", "info": info})


def _multipart(body: bytes, ctype: str) -> dict:
    boundary = ctype.split("boundary=")[1].encode()
    out = {}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, content = part.split(b"\r\n\r\n", 1)
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        out[name] = content[:-2] if content.endswith(b"\r\n") else content
    return out


class FakeQbit:
    def __init__(self, categories=("movies", "mags")):
        self.categories = list(categories)
        self.calls = []
        self.added = []  # dicts of the add form
        self.deleted = []  # (hash, deleteFiles)
        self.queue_moves = []  # (path, hashes)
        self.recategorized = []  # (hashes, category)
        self.logged_in = False
        self.fail_login = False
        self.torrents = {
            FOREIGN_HASH: {
                "hash": FOREIGN_HASH,
                "name": "Some Movie",
                "category": "movies",
                "state": "uploading",
                "progress": 1.0,
                "content_path": "/data/movies/Some Movie",
            }
        }

    def set_state(self, job, state, progress, content_path=None):
        t = self.torrents[job]
        t["state"], t["progress"] = state, progress
        if content_path:
            t["content_path"] = content_path

    def __call__(self, method, url, headers, body, timeout):
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append({"method": method, "path": path, "query": query, "headers": headers})
        assert "preferences" not in path and "setShareLimits" not in path, path
        assert headers.get("Referer") == QB_URL, "qBittorrent wants a matching Referer"
        if path == "/api/v2/auth/login":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if self.fail_login or form != {"username": QB_USER, "password": QB_PASS}:
                return 200, b"Fails.", {}
            self.logged_in = True
            return 200, b"Ok.", {"Set-Cookie": "SID=abc123; HttpOnly; path=/"}
        if headers.get("Cookie") != "SID=abc123":
            return 403, b"Forbidden", {}
        if path == "/api/v2/app/version":
            return 200, b"v5.0.1", {}
        if path == "/api/v2/torrents/categories":
            return (
                200,
                json.dumps({c: {"name": c, "savePath": ""} for c in self.categories}).encode(),
                {},
            )
        if path == "/api/v2/torrents/add":
            form = _multipart(body, headers["Content-Type"])
            self.added.append(form)
            if "torrents" in form:
                job = info_hash(form["torrents"])
            else:
                job = magnet_hash(form["urls"].decode())
            self.torrents[job] = {
                "hash": job,
                "name": "added",
                "category": form["category"].decode(),
                "state": "metaDL",
                "progress": 0.0,
                "size": 1000,
                "amount_left": 1000,
                "eta": 8640000,
            }
            return 200, b"Ok.", {}
        if path == "/api/v2/torrents/info":
            # Like qBittorrent: answers with whatever hashes it is asked for, in any
            # category, so the client must filter.
            wanted = set(query.get("hashes", "").split("|")) | {FOREIGN_HASH}
            rows = [t for h, t in self.torrents.items() if h in wanted]
            return 200, json.dumps(rows).encode(), {}
        if path in (
            "/api/v2/torrents/topPrio",
            "/api/v2/torrents/bottomPrio",
            "/api/v2/torrents/setForceStart",
        ):
            form = dict(urllib.parse.parse_qsl(body.decode()))
            self.queue_moves.append((path.rsplit("/", 1)[1], form["hashes"]))
            return 200, b"", {}
        if path == "/api/v2/torrents/setCategory":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            self.recategorized.append((form["hashes"], form["category"]))
            self.torrents[form["hashes"]]["category"] = form["category"]
            return 200, b"", {}
        if path == "/api/v2/torrents/delete":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            self.deleted.append((form["hashes"], form["deleteFiles"]))
            self.torrents.pop(form["hashes"], None)
            return 200, b"", {}
        return 404, b"Not Found", {}
