"""A direct Newznab indexer (the API most usenet indexers speak).

Built on the Prowlarr client's request code, so the same rules apply: PeriodiCool's
User-Agent, no automatic redirects, the key only ever sent to the indexer's own
host, errors as readable sentences, and every call recorded for limits.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace

from periodicool.prowlarr import (
    ApiLimits,
    FoundRelease,
    ProwlarrClient,
    ProwlarrError,
    parse_newznab,
)


class NewznabClient(ProwlarrClient):
    kind = "newznab"
    protocol = "usenet"

    def __init__(self, url: str, api_key: str, name: str = "Newznab", **kwargs) -> None:
        # People paste either the site ("https://api.example") or the endpoint (".../api").
        url = url.rstrip("/")
        if url.lower().endswith("/api"):
            url = url[: -len("/api")]
        super().__init__(url, api_key, **kwargs)
        self.name = name
        self.last_limits = ApiLimits()

    def _api(self, params: list, kind: str) -> bytes:
        return self._get("/api", params, kind, None, key_in_query=True)

    def caps(self) -> dict:
        """What the indexer offers: its name, whether search works, and categories."""
        body = self._api([("t", "caps")], "caps")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ProwlarrError("the indexer did not answer with Newznab capabilities") from exc
        if root.tag == "error":
            raise ProwlarrError(f"indexer error {root.get('code')}: {root.get('description')}")
        server = root.find("server")
        search = root.find("searching/search")
        categories = [
            (int(c.get("id")), c.get("name", ""))
            for c in root.iter("category")
            if (c.get("id") or "").isdigit()
        ]
        return {
            "title": server.get("title") if server is not None else None,
            "search": search is None or search.get("available", "yes") == "yes",
            "categories": categories,
            "magazines": any(cid == 7010 or cid == 7000 for cid, _ in categories),
        }

    def _found(self, body: bytes) -> list[FoundRelease]:
        found, limits = parse_newznab(body, None, self.protocol)
        self.last_limits = limits
        return [replace(f, indexer=self.name) for f in found]

    def search(self, term: str, categories: list[int], indexer_ids=None) -> list[FoundRelease]:
        params = [
            ("t", "search"),
            ("q", term),
            ("cat", ",".join(map(str, categories))),
            ("extended", 1),
        ]
        return self._found(self._api(params, "search"))

    def rss(self, indexer_id, categories: list[int]) -> tuple[list[FoundRelease], ApiLimits]:
        params = [("t", "search"), ("cat", ",".join(map(str, categories))), ("extended", 1)]
        found = self._found(self._api(params, "rss"))
        return found, self.last_limits


class TorznabClient(NewznabClient):
    """A direct Torznab indexer (Jackett, or a tracker's own Torznab feed): the
    Newznab API with torrent results (seeders, peers, magnet links)."""

    kind = "torznab"
    protocol = "torrent"

    def __init__(self, url: str, api_key: str, name: str = "Torznab", **kwargs) -> None:
        super().__init__(url, api_key, name=name, **kwargs)
