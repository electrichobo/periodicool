"""Collect release names for the dev magazines from Prowlarr into tests/fixtures.

One search per query, spaced out by --delay, and cached so re-runs cost no API
hits unless --refresh is given. Only names, sizes, dates and categories are
saved: no download links, GUIDs, indexer names or keys.

Usage:
    PROWLARR_API_KEY=... python scripts/collect_fixtures.py --url http://prowlarr:9696
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

CATEGORY = 7010  # Books/Mags

# (fixture slug, magazine, query). Aliases get their own query.
QUERIES = [
    ("american-cinematographer", "American Cinematographer", "American Cinematographer"),
    ("amazonas", "AMAZONAS Magazine", "Amazonas"),
    ("2600", "2600: The Hacker Quarterly", "2600"),
    ("linux-magazine", "Linux Magazine", "Linux Magazine"),
    ("linux-pro-magazine", "Linux Magazine", "Linux Pro Magazine"),
    ("make", "Make: Magazine", "Make Magazine"),
    ("fortean-times", "Fortean Times", "Fortean Times"),
    ("tabletop-gaming", "Tabletop Gaming Magazine", "Tabletop Gaming"),
    ("people", "People", "People"),
]

ROOT = Path(__file__).resolve().parent.parent


def search(url: str, key: str, query: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {"query": query, "categories": CATEGORY, "type": "search", "limit": 100}
    )
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/v1/search?{params}", headers={"X-Api-Key": key}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def clean(results: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for r in results:
        title = (r.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen[title] = {
            "title": title,
            "size": r.get("size"),
            "publish_date": r.get("publishDate"),
            "categories": sorted({c["id"] for c in r.get("categories", []) if "id" in c}),
        }
    return sorted(seen.values(), key=lambda r: r["title"].lower())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("PROWLARR_URL"), help="Prowlarr base URL")
    ap.add_argument("--out", type=Path, default=ROOT / "tests" / "fixtures" / "releases")
    ap.add_argument("--cache", type=Path, default=ROOT / "dev-data" / "fixture-cache")
    ap.add_argument("--delay", type=float, default=10.0, help="seconds between searches")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--only", nargs="*", help="limit to these fixture slugs")
    args = ap.parse_args()

    key = os.environ.get("PROWLARR_API_KEY", "")
    if not args.url or not key:
        print("Set --url (or PROWLARR_URL) and PROWLARR_API_KEY.", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    first = True
    for slug, magazine, query in QUERIES:
        if args.only and slug not in args.only:
            continue
        cached = args.cache / f"{slug}.json"
        if cached.exists() and not args.refresh:
            releases = json.loads(cached.read_text(encoding="utf-8"))
            source = "cache"
        else:
            if not first:
                time.sleep(args.delay)
            first = False
            releases = clean(search(args.url, key, query))
            cached.write_text(json.dumps(releases, indent=2), encoding="utf-8")
            source = "prowlarr"
        fixture = {
            "magazine": magazine,
            "query": query,
            "category": CATEGORY,
            "collected": date.today().isoformat(),
            "releases": releases,
        }
        (args.out / f"{slug}.json").write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"{slug:26} {len(releases):4} releases ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
