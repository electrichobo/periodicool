"""Library of Congress catalog, through its public SRU search (MARC records)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from periodicool.issn import normalize
from periodicool.lookups import LookupError, Record
from periodicool.lookups.http import get

URL = "http://lx2.loc.gov:210/lcdb"
_MARC = "{http://www.loc.gov/MARC21/slim}"

# Frequency words in MARC 310 to our frequencies. Only clear cases.
_FREQUENCIES = (
    ("biweekly", "biweekly"),
    ("bimonthly", "bimonthly"),
    ("semimonthly", None),
    ("weekly", "weekly"),
    ("monthly", "monthly"),
    ("quarterly", "quarterly"),
    ("13 no", "thirteen_per_year"),
    ("thirteen", "thirteen_per_year"),
)


def frequency_guess(text: str | None) -> str | None:
    lowered = (text or "").lower()
    for words, value in _FREQUENCIES:
        if words in lowered:
            return value
    return None


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(" ./:;,=") or None


def _subfields(df, codes: str) -> str | None:
    parts = [s.text or "" for s in df if s.get("code") in codes]
    return _clean(" ".join(p.strip() for p in parts if p.strip()))


def parse(body: bytes) -> Record | None:
    found = parse_all(body)
    return found[0] if found else None


def parse_all(body: bytes) -> list[Record]:
    """Every MARC record in an answer, in the order the catalog gave them."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise LookupError("Library of Congress sent something that is not XML") from exc
    return [_record(r) for r in root.iter(f"{_MARC}record")]


def is_serial(record) -> bool:
    """MARC leader position 7 is "s" for serials (magazines, journals)."""
    leader = record.findtext(f"{_MARC}leader") or ""
    return len(leader) > 7 and leader[7] == "s"


def _record(record) -> Record:
    out = Record(source="loc")
    for cf in record.iter(f"{_MARC}controlfield"):
        if cf.get("tag") == "001":
            out.ref = (cf.text or "").strip() or None
        if cf.get("tag") == "008" and cf.text and len(cf.text) > 11 and cf.text[7:11].isdigit():
            out.start_year = int(cf.text[7:11])
    for df in record.iter(f"{_MARC}datafield"):
        tag = df.get("tag")
        if tag == "022":
            for s in df:
                code = normalize(s.text) if s.get("code") in ("a", "l") else None
                if code and code not in out.issns:
                    out.issns.append(code)
        elif tag == "245" and out.title is None:
            out.title = _subfields(df, "abnp")
        elif tag == "246":
            if (alt := _subfields(df, "a")) and alt not in out.alt_titles:
                out.alt_titles.append(alt)
        elif tag in ("260", "264") and out.publisher is None:
            out.publisher = _subfields(df, "b")
        elif tag == "310":
            out.frequency_text = _subfields(df, "ab")
        elif tag == "321" and out.former_frequency_text is None:
            out.former_frequency_text = _subfields(df, "ab")
        elif tag == "362" and out.start is None:
            out.start = _subfields(df, "a")
    out.frequency = frequency_guess(out.frequency_text)
    return out


def lookup(issn: str | None = None, title: str | None = None, transport=None) -> Record | None:
    found = candidates(issn, title, transport, limit=1)
    return found[0] if found else None


def candidates(
    issn: str | None = None, title: str | None = None, transport=None, limit: int = 5
) -> list[Record]:
    """Up to `limit` catalog records for an ISSN or a title."""
    if issn:
        query = f"bath.issn={issn}"
    elif title:
        query = 'dc.title="' + title.replace('"', " ") + '"'
    else:
        return []
    body = get(
        "loc",
        URL,
        {
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": query,
            "maximumRecords": str(limit if issn else limit * 4),  # books are left out
            "recordSchema": "marcxml",
        },
        accept="text/xml",
        transport=transport,
    )
    if issn:
        return parse_all(body)[:limit]
    # A title search also finds books with that title: keep magazines and journals.
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise LookupError("Library of Congress sent something that is not XML") from exc
    return [_record(r) for r in root.iter(f"{_MARC}record") if is_serial(r)][:limit]
