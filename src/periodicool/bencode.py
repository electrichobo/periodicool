"""Just enough of BitTorrent's bencoding to read a .torrent file: its info hash (the
ID qBittorrent uses for it) and the names of its files. Magnet links give their
hash directly. Stdlib only."""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse


class BencodeError(ValueError):
    pass


def _decode(data: bytes, i: int, spans: dict | None, depth: int = 0):
    """Decode one value at i. Returns (value, next index). Records where the top-level
    "info" value starts and ends in spans, for the info hash."""
    if depth > 64:
        raise BencodeError("nested too deeply")
    if i >= len(data):
        raise BencodeError("truncated")
    c = data[i : i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        try:
            return int(data[i + 1 : end]), end + 1
        except ValueError as exc:
            raise BencodeError("bad integer") from exc
    if c == b"l":
        i += 1
        out = []
        while data[i : i + 1] != b"e":
            value, i = _decode(data, i, None, depth + 1)
            out.append(value)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i : i + 1] != b"e":
            key, i = _decode(data, i, None, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError("dictionary key is not a string")
            start = i
            value, i = _decode(data, i, None, depth + 1)
            if spans is not None and depth == 0 and key == b"info":
                spans["info"] = (start, i)
            out[key] = value
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        if start + length > len(data):
            raise BencodeError("truncated string")
        return data[start : start + length], start + length
    raise BencodeError(f"unexpected byte {c!r} at {i}")


def decode_torrent(data: bytes) -> tuple[dict, bytes]:
    """The decoded torrent and the raw bytes of its info dictionary."""
    if not data.startswith(b"d"):
        raise BencodeError("not a torrent file")
    spans: dict = {}
    try:
        meta, _ = _decode(data, 0, spans)
    except (ValueError, IndexError) as exc:
        raise BencodeError(f"not a torrent file: {exc}") from exc
    if "info" not in spans or not isinstance(meta.get(b"info"), dict):
        raise BencodeError("torrent file has no info section")
    start, end = spans["info"]
    return meta, data[start:end]


def info_hash(data: bytes) -> str:
    """The torrent's hash as qBittorrent shows it: SHA-1 of the info dictionary, or for a
    v2-only torrent the SHA-256 cut to 40 hex characters."""
    meta, raw = decode_torrent(data)
    info = meta[b"info"]
    if b"pieces" not in info and info.get(b"meta version") == 2:
        return hashlib.sha256(raw).hexdigest()[:40]
    return hashlib.sha1(raw).hexdigest()  # noqa: S324  the BitTorrent v1 info hash is SHA-1


def file_names(data: bytes) -> list[str]:
    meta, _ = decode_torrent(data)
    info = meta[b"info"]
    if b"files" in info:
        return [
            "/".join(p.decode("utf-8", "replace") for p in f.get(b"path", []))
            for f in info[b"files"]
            if isinstance(f, dict)
        ]
    if b"file tree" in info:  # v2
        names: list[str] = []

        def walk(tree, prefix):
            for key, value in tree.items():
                if key == b"":
                    names.append(prefix)
                elif isinstance(value, dict):
                    walk(value, f"{prefix}/{key.decode('utf-8', 'replace')}".lstrip("/"))

        walk(info[b"file tree"], "")
        return names
    return [info.get(b"name", b"").decode("utf-8", "replace")]


def magnet_hash(uri: str) -> str:
    """The hash in a magnet link (btih hex or base32, or a v2 btmh), lower-case hex."""
    if not uri.lower().startswith("magnet:?"):
        raise BencodeError("not a magnet link")
    for key, value in urllib.parse.parse_qsl(uri[len("magnet:?") :]):
        if key != "xt":
            continue
        if m := re.fullmatch(r"urn:btih:([0-9a-fA-F]{40})", value):
            return m.group(1).lower()
        if m := re.fullmatch(r"urn:btih:([A-Za-z2-7]{32})", value):
            return base64.b32decode(m.group(1).upper()).hex()
        if m := re.fullmatch(r"urn:btmh:1220([0-9a-fA-F]{64})", value):
            return m.group(1).lower()[:40]
    raise BencodeError("magnet link has no torrent hash")
