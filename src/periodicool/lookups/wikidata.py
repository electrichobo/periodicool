"""Wikidata (CC0): canonical title, aliases, ISSNs, start, language, country, website."""

from __future__ import annotations

import json
import re

from periodicool.issn import normalize
from periodicool.lookups import LookupError, Record
from periodicool.lookups.http import get

SPARQL_URL = "https://query.wikidata.org/sparql"
API_URL = "https://www.wikidata.org/w/api.php"
# Kinds of item accepted when searching by title: periodical, magazine, journal,
# and the subclasses Wikidata uses for consumer and trade magazines.
_PERIODICALS = "wd:Q1002697 wd:Q41298 wd:Q5633421 wd:Q737498 wd:Q11032"

_QUERY = """
SELECT ?item ?itemLabel ?alias ?issn ?inception ?langLabel ?countryLabel ?publisherLabel ?website
WHERE {{
  {where}
  OPTIONAL {{ ?item skos:altLabel ?alias FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?item wdt:P236 ?issn }}
  OPTIONAL {{ ?item wdt:P571 ?inception }}
  OPTIONAL {{ ?item wdt:P407 ?lang }}
  OPTIONAL {{ ?item wdt:P495 ?country }}
  OPTIONAL {{ ?item wdt:P123 ?publisher }}
  OPTIONAL {{ ?item wdt:P856 ?website }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 200
"""


def _sparql(where: str, transport=None) -> list[dict]:
    body = get(
        "wikidata",
        SPARQL_URL,
        {"query": _QUERY.format(where=where), "format": "json"},
        accept="application/sparql-results+json",
        transport=transport,
    )
    try:
        data = json.loads(body)
        return [{k: v["value"] for k, v in b.items()} for b in data["results"]["bindings"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError("Wikidata sent an answer PeriodiCool cannot read") from exc


def parse(rows: list[dict]) -> Record | None:
    if not rows:
        return None
    first_item = rows[0].get("item")
    rows = [r for r in rows if r.get("item") == first_item]  # one item only
    out = Record(source="wikidata", ref=first_item.rsplit("/", 1)[-1] if first_item else None)
    for r in rows:
        out.title = out.title or r.get("itemLabel")
        if (alias := r.get("alias")) and alias not in out.alt_titles:
            out.alt_titles.append(alias)
        if (code := normalize(r.get("issn"))) and code not in out.issns:
            out.issns.append(code)
        when = r.get("inception") or ""
        if out.start_year is None and (m := re.match(r"(\d{4})", when)):
            out.start_year = int(m.group(1))
            out.start = when[:10]
        out.language = out.language or r.get("langLabel")
        out.country = out.country or r.get("countryLabel")
        out.publisher = out.publisher or r.get("publisherLabel")
        out.website = out.website or r.get("website")
    return out


def lookup(issn: str | None = None, title: str | None = None, transport=None) -> Record | None:
    if issn:
        if normalize(issn) is None:
            return None
        return parse(_sparql(f'?item wdt:P236 "{normalize(issn)}".', transport))
    if not title:
        return None
    body = get(
        "wikidata",
        API_URL,
        {
            "action": "wbsearchentities",
            "search": title,
            "language": "en",
            "type": "item",
            "limit": "7",
            "format": "json",
        },
        accept="application/json",
        transport=transport,
    )
    try:
        ids = [hit["id"] for hit in json.loads(body).get("search", [])]
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError("Wikidata sent an answer PeriodiCool cannot read") from exc
    ids = [i for i in ids if re.fullmatch(r"Q\d+", i)]
    if not ids:
        return None
    values = " ".join(f"wd:{i}" for i in ids)
    where = (
        f"VALUES ?item {{ {values} }} VALUES ?kind {{ {_PERIODICALS} }} "
        "?item wdt:P31/wdt:P279* ?kind."
    )
    return parse(_sparql(where, transport))
