"""Prowlarr client (stdlib only).

Calls are spaced at least `min_interval` seconds apart. The API key travels in
a header where Prowlarr allows it; every URL or error that leaves this module
goes through `redact()` first, and download URLs are stored without the key.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from periodicool import USER_AGENT

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
log = logging.getLogger(__name__)
_APIKEY_PARAM = re.compile(r"(apikey=)[^&\s\"']+", re.IGNORECASE)

Transport = Callable[[str, dict, float], tuple[int, bytes]]
CallHook = Callable[..., None]


class ProwlarrError(Exception):
    pass


class RateLimited(ProwlarrError):
    """Prowlarr or an indexer said to slow down (429/503)."""


@dataclass(frozen=True)
class FoundRelease:
    guid: str
    title: str
    size: int | None = None
    publish_date: datetime | None = None
    indexer_id: int | None = None
    indexer: str | None = None
    download_url: str | None = None  # without the API key
    info_url: str | None = None
    categories: tuple[int, ...] = ()
    protocol: str | None = "usenet"
    seeders: int | None = None  # torrents only
    peers: int | None = None


@dataclass(frozen=True)
class IndexerInfo:
    id: int
    name: str
    protocol: str
    enabled: bool
    supports_category: bool
    query_limit: int | None = None
    grab_limit: int | None = None


@dataclass
class ApiLimits:
    api_current: int | None = None
    api_max: int | None = None
    grab_current: int | None = None
    grab_max: int | None = None
    extra: dict = field(default_factory=dict)


def strip_apikey(url: str | None) -> str | None:
    if not url or url.lower().startswith("magnet:"):
        return url  # a magnet link carries no API key; kept exactly as given
    parts = urllib.parse.urlsplit(url)
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "apikey"
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are never followed automatically: urllib would copy our headers
    (including an API key) onto the request to the new host."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib_transport(url: str, headers: dict, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers or {})


def describe_error(body: bytes) -> str:
    """A readable line from an error body: JSON title and detail, or plain text."""
    text = body[:4000].decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        parts = [str(data[k]) for k in ("title", "message", "error", "detail") if data.get(k)]
        line = ": ".join(dict.fromkeys(parts)) or text[:200]
        if "cloudflare" in text.lower():
            line += " (Cloudflare in front of the indexer refused the request)"
        return line[:300]
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value[:4].isdigit():
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsedate_to_datetime(value)
    except ValueError, TypeError:
        return None


def _int(value) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


class ProwlarrClient:
    kind = "prowlarr"
    # Set by sources.build_indexer: the list entry this client belongs to.
    source_id: int | None = None
    source_name: str | None = None
    row = None

    def __init__(
        self,
        url: str,
        api_key: str,
        transport: Transport | None = None,
        min_interval: float = 2.0,
        timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_call: CallHook | None = None,
    ) -> None:
        if not url or not api_key:
            raise ProwlarrError("Prowlarr URL and API key are required")
        self.url = url.rstrip("/")
        self._key = api_key
        self._transport = transport or _urllib_transport
        self.min_interval = min_interval
        self.timeout = timeout
        self._clock, self._sleep = clock, sleep
        self._last: float | None = None
        self.on_call = on_call

    def redact(self, text: str) -> str:
        text = _APIKEY_PARAM.sub(r"\1REDACTED", str(text))
        return text.replace(self._key, "REDACTED") if self._key else text

    def _get(
        self,
        path: str,
        params: list[tuple[str, object]],
        kind: str,
        indexer_id=None,
        key_in_query: bool = False,
        follow_once: bool = False,
    ) -> bytes:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        headers = {"Accept": "application/json, application/xml"}
        if key_in_query:
            params = [*params, ("apikey", self._key)]
        else:
            headers["X-Api-Key"] = self._key
        url = f"{self.url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            status, body, reply = self._send(url, headers)
            where = "Prowlarr"
            if follow_once and status in (301, 302, 303, 307, 308):
                status, body, where = self._follow(url, reply, kind)
        except OSError as exc:
            self._last = self._clock()
            self._hook(kind, indexer_id, None, False, self.redact(exc))
            raise ProwlarrError(f"cannot reach Prowlarr: {self.redact(exc)}") from None
        self._last = self._clock()
        ok = 200 <= status < 300
        note = None if ok else self.redact(describe_error(body))
        self._hook(kind, indexer_id, status, ok, note)
        log.debug("Prowlarr %s %s: %s", kind, self.redact(path), status)
        if status in (429, 503):
            raise RateLimited(f"{where} returned {status} for {kind}")
        if not ok:
            raise ProwlarrError(f"{where} returned {status} for {kind}: {note}")
        return body

    def _send(self, url: str, headers: dict):
        result = self._transport(url, headers, self.timeout)
        return result[0], result[1], (result[2] if len(result) > 2 else {})

    def _follow(self, url: str, reply: dict, kind: str):
        """Follow one redirect from Prowlarr (an indexer set to "Redirect"), as Sonarr
        does. Only our User-Agent goes along: never the Prowlarr key or headers. A
        redirect to a magnet link is the answer itself (torrent indexers do this)."""
        location = {k.lower(): v for k, v in reply.items()}.get("location")
        if not location:
            return 502, b"Prowlarr sent a redirect without a location", "Prowlarr"
        if location.lower().startswith("magnet:"):
            return 200, location.encode(), "Prowlarr"
        target = urllib.parse.urljoin(url, location)
        if urllib.parse.urlsplit(target).scheme not in ("http", "https"):
            return 502, b"Prowlarr redirected to an unsupported address", "Prowlarr"
        host = urllib.parse.urlsplit(target).hostname or "the indexer"
        log.info("Prowlarr redirected the %s to %s; following once", kind, host)
        status, body, again = self._send(target, {"Accept": "application/x-nzb, */*"})
        moved = {k.lower(): v for k, v in again.items()}.get("location", "")
        if status in (301, 302, 303, 307, 308) and moved.lower().startswith("magnet:"):
            return 200, moved.encode(), f"the indexer ({host})"
        return status, body, f"the indexer ({host})"

    def _hook(self, kind, indexer_id, status, ok, note) -> None:
        if self.on_call:
            self.on_call(kind=kind, indexer_id=indexer_id, status=status, ok=ok, note=note)

    def _json(self, path, params, kind, indexer_id=None):
        body = self._get(path, params, kind, indexer_id)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ProwlarrError(f"bad JSON from {kind}") from exc

    # API

    def search(
        self,
        query: str,
        categories: list[int],
        indexer_ids: list[int] | None = None,
        limit: int = 100,
    ) -> list[FoundRelease]:
        params: list[tuple[str, object]] = [("query", query), ("type", "search"), ("limit", limit)]
        params += [("categories", c) for c in categories]
        params += [("indexerIds", i) for i in indexer_ids or ()]
        rows = self._json("/api/v1/search", params, "search")
        return [self._from_json(r) for r in rows if r.get("title")]

    def _from_json(self, r: dict) -> FoundRelease:
        return FoundRelease(
            guid=str(r.get("guid") or r.get("downloadUrl") or r["title"]),
            title=r["title"],
            size=_int(r.get("size")),
            publish_date=_parse_date(r.get("publishDate")),
            indexer_id=_int(r.get("indexerId")),
            indexer=r.get("indexer"),
            download_url=strip_apikey(r.get("downloadUrl") or r.get("magnetUrl")),
            info_url=strip_apikey(r.get("infoUrl")),
            categories=tuple(sorted({c["id"] for c in r.get("categories", []) if "id" in c})),
            protocol=r.get("protocol"),
            seeders=_int(r.get("seeders")),
            peers=_int(r.get("leechers")),
        )

    def rss(self, indexer_id: int, categories: list[int]) -> tuple[list[FoundRelease], ApiLimits]:
        """The indexer's latest releases, through Prowlarr's Newznab endpoint."""
        params = [("t", "search"), ("cat", ",".join(map(str, categories))), ("extended", 1)]
        body = self._get(f"/{indexer_id}/api", params, "rss", indexer_id, key_in_query=True)
        return parse_newznab(body, indexer_id)

    def fetch_nzb(self, download_url: str, indexer_id: int | None = None) -> bytes:
        """Download an NZB through Prowlarr (usenet only; see fetch_download)."""
        kind, body = self.fetch_download(download_url, indexer_id)
        if kind != "nzb":
            raise ProwlarrError("download is not an NZB file")
        return body

    def fetch_download(self, download_url: str, indexer_id: int | None = None):
        """Download what a release points at: ("nzb", bytes), ("torrent", bytes) or
        ("magnet", link). Counts as a grab on the indexer.

        The API key is added only when the URL points at the configured Prowlarr (or
        indexer), so it is never sent anywhere else.
        """
        if download_url.lower().startswith("magnet:"):
            if "xt=" not in download_url:
                raise ProwlarrError("magnet link without a torrent hash")
            return "magnet", download_url
        target = urllib.parse.urlsplit(download_url)
        base = urllib.parse.urlsplit(self.url)

        def origin(u):
            port = u.port or (443 if u.scheme == "https" else 80)
            return u.scheme, (u.hostname or "").lower(), port

        if origin(target) != origin(base):
            raise ProwlarrError("download URL is not on the configured Prowlarr; not fetched")
        path = target.path
        prefix = base.path.rstrip("/")
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        params = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(target.query, keep_blank_values=True)
            if k.lower() != "apikey"
        ]
        body = self._get(path, params, "grab", indexer_id, key_in_query=True, follow_once=True)
        if body[:7].lower() == b"magnet:":
            link = body.decode("utf-8", "replace").strip()
            if "xt=" not in link:
                raise ProwlarrError("magnet link without a torrent hash")
            return "magnet", link
        if b"<nzb" in body[:2048]:
            return "nzb", body
        if body[:1] == b"d" and b"4:info" in body:
            return "torrent", body
        raise ProwlarrError("download is neither an NZB nor a torrent file")

    def indexers(self, category: int = 7010) -> list[IndexerInfo]:
        out = []
        for r in self._json("/api/v1/indexer", [], "indexers"):
            caps = r.get("capabilities", {}).get("categories", [])
            ids = {c["id"] for c in caps} | {
                s["id"] for c in caps for s in c.get("subCategories", [])
            }
            fields = {f["name"]: f.get("value") for f in r.get("fields", [])}
            out.append(
                IndexerInfo(
                    id=r["id"],
                    name=r.get("name", f"indexer {r['id']}"),
                    protocol=r.get("protocol", ""),
                    enabled=bool(r.get("enable")),
                    supports_category=category in ids or (category // 1000 * 1000) in ids,
                    query_limit=_int(fields.get("baseSettings.queryLimit")),
                    grab_limit=_int(fields.get("baseSettings.grabLimit")),
                )
            )
        return out

    def indexer_status(self) -> dict[int, datetime | None]:
        """Indexers Prowlarr has disabled for now, with the time they come back."""
        rows = self._json("/api/v1/indexerstatus", [], "status")
        return {r["indexerId"]: _parse_date(r.get("disabledTill")) for r in rows}

    def indexer_stats(self, start: datetime) -> dict[int, dict]:
        """Usage since start. Prowlarr counts up to its own clock, so no end date is sent."""
        params = [("startDate", start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))]
        data = self._json("/api/v1/indexerstats", params, "stats")
        return {
            r["indexerId"]: {
                "queries": _int(r.get("numberOfQueries")) or 0,
                "rss": _int(r.get("numberOfRssQueries")) or 0,
                "grabs": _int(r.get("numberOfGrabs")) or 0,
            }
            for r in data.get("indexers", [])
        }


def parse_newznab(
    body: bytes, indexer_id: int | None = None, protocol: str = "usenet"
) -> tuple[list[FoundRelease], ApiLimits]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProwlarrError("bad XML from RSS feed") from exc
    if root.tag == "error":
        raise ProwlarrError(f"indexer error {root.get('code')}: {root.get('description')}")
    limits = ApiLimits()
    lim = root.find(f".//{NEWZNAB_NS}apilimits")
    if lim is not None:
        limits = ApiLimits(
            api_current=_int(lim.get("apicurrent")),
            api_max=_int(lim.get("apimax")),
            grab_current=_int(lim.get("grabcurrent")),
            grab_max=_int(lim.get("grabmax")),
        )
    channel = root.find("channel")
    indexer_name = channel.findtext("title") if channel is not None else None
    found = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        attrs = {}
        cats = set()
        for a in [*item.findall(f"{NEWZNAB_NS}attr"), *item.findall(f"{TORZNAB_NS}attr")]:
            if a.get("name") == "category":
                if (c := _int(a.get("value"))) is not None:
                    cats.add(c)
            else:
                attrs[a.get("name")] = a.get("value")
        enclosure = item.find("enclosure")
        size = _int(attrs.get("size")) or (
            _int(enclosure.get("length")) if enclosure is not None else None
        )
        link = (
            item.findtext("link")
            or (enclosure.get("url") if enclosure is not None else None)
            or attrs.get("magneturl")
        )
        found.append(
            FoundRelease(
                guid=(item.findtext("guid") or link or title).strip(),
                title=title,
                size=size,
                publish_date=_parse_date(item.findtext("pubDate")),
                indexer_id=indexer_id,
                indexer=indexer_name,
                download_url=strip_apikey(link),
                info_url=strip_apikey(item.findtext("comments")),
                categories=tuple(sorted(cats)),
                protocol=protocol,
                seeders=_int(attrs.get("seeders")),
                peers=_int(attrs.get("peers")),
            )
        )
    return found, limits
