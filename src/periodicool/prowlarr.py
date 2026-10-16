"""Prowlarr client (stdlib only).

Calls are spaced at least `min_interval` seconds apart. The API key travels in
a header where Prowlarr allows it; every URL or error that leaves this module
goes through `redact()` first, and download URLs are stored without the key.
"""

from __future__ import annotations

import json
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

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
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
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "apikey"
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _urllib_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


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
            status, body = self._transport(url, headers, self.timeout)
        except OSError as exc:
            self._last = self._clock()
            self._hook(kind, indexer_id, None, False, self.redact(exc))
            raise ProwlarrError(f"cannot reach Prowlarr: {self.redact(exc)}") from None
        self._last = self._clock()
        ok = 200 <= status < 300
        self._hook(kind, indexer_id, status, ok, None if ok else self.redact(body[:200]))
        if status in (429, 503):
            raise RateLimited(f"Prowlarr returned {status} for {kind}")
        if not ok:
            raise ProwlarrError(f"Prowlarr returned {status} for {kind}: {self.redact(body[:200])}")
        return body

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
            download_url=strip_apikey(r.get("downloadUrl")),
            info_url=strip_apikey(r.get("infoUrl")),
            categories=tuple(sorted({c["id"] for c in r.get("categories", []) if "id" in c})),
            protocol=r.get("protocol"),
        )

    def rss(self, indexer_id: int, categories: list[int]) -> tuple[list[FoundRelease], ApiLimits]:
        """The indexer's latest releases, through Prowlarr's Newznab endpoint."""
        params = [("t", "search"), ("cat", ",".join(map(str, categories))), ("extended", 1)]
        body = self._get(f"/{indexer_id}/api", params, "rss", indexer_id, key_in_query=True)
        return parse_newznab(body, indexer_id)

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
    body: bytes, indexer_id: int | None = None
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
        for a in item.findall(f"{NEWZNAB_NS}attr"):
            if a.get("name") == "category":
                if (c := _int(a.get("value"))) is not None:
                    cats.add(c)
            else:
                attrs[a.get("name")] = a.get("value")
        enclosure = item.find("enclosure")
        size = _int(attrs.get("size")) or (
            _int(enclosure.get("length")) if enclosure is not None else None
        )
        link = item.findtext("link") or (enclosure.get("url") if enclosure is not None else None)
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
                protocol="usenet",
            )
        )
    return found, limits
