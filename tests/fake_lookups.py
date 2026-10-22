"""A fake transport for the metadata lookups, replaying recorded public responses."""

import json
import urllib.parse
from pathlib import Path

RECORDED = Path(__file__).parent / "fixtures" / "lookups" / "recorded.json"


def key_for(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
    if parts.netloc.startswith("lx2.loc.gov"):
        return f"loc {q.get('query', '')}"
    if parts.netloc == "query.wikidata.org":
        query = q.get("query", "")
        if 'wdt:P236 "' in query:
            return "wikidata issn " + query.split('wdt:P236 "')[1].split('"')[0]
        return "wikidata items " + query.split("VALUES ?item {")[1].split("}")[0].strip()
    if parts.netloc == "www.wikidata.org":
        return f"wikidata search {q.get('search', '')}"
    if parts.netloc == "archive.org":
        return f"archive {q.get('q', '')}"
    return f"other {url}"


class FakeLookups:
    def __init__(self, responses: dict | None = None):
        self.responses = responses if responses is not None else load()
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "key": key_for(url)})
        body = self.responses.get(key_for(url))
        if body is None:
            return 404, b""
        return 200, body.encode("utf-8")


def load() -> dict:
    return json.loads(RECORDED.read_text(encoding="utf-8"))
