"""Internet Archive search, for lists of past issues. Metadata only: item titles and
dates are read; no files are ever fetched."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from periodicool.lookups import LookupError
from periodicool.lookups.http import get

URL = "https://archive.org/advancedsearch.php"
_ISSUE_FIELDS = (
    "cover_date",
    "year",
    "month",
    "month_end",
    "day",
    "season",
    "issue_number",
    "volume_number",
    "special",
)
MAX_ITEMS = 200


@dataclass
class BackIssue:
    key: str
    label: str
    cover_date: str | None
    issue_number: int | None
    volume_number: int | None
    source_title: str
    identifier: str
    fields: dict  # issue fields, dates as ISO text

    def as_dict(self) -> dict:
        return asdict(self)


def search(title: str, transport=None) -> list[dict]:
    """Item titles and dates for a magazine title (texts only)."""
    safe = title.replace('"', " ")
    body = get(
        "archive",
        URL,
        {
            "q": f'title:("{safe}") AND mediatype:texts',
            "fl[]": ["identifier", "title", "date"],
            "rows": str(MAX_ITEMS),
            "sort[]": "date desc",
            "output": "json",
        },
        accept="application/json",
        transport=transport,
    )
    try:
        docs = json.loads(body)["response"]["docs"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError("the Internet Archive sent an answer PeriodiCool cannot read") from exc
    return [
        {"identifier": d.get("identifier", ""), "title": d.get("title", ""), "date": d.get("date")}
        for d in docs
        if isinstance(d.get("title"), str)
    ]


def back_issues(mag, items: list[dict]) -> tuple[list[BackIssue], int]:
    """Parse item titles into issues of this magazine. Returns (issues, skipped count).

    Only confident parses are kept; one per issue key.
    """
    from periodicool.matching import TitleIndex, match_title
    from periodicool.naming import date_label
    from periodicool.parser import parse_release
    from periodicool.schedule import identify

    titles = TitleIndex.from_magazines([mag])
    found: dict[str, BackIssue] = {}
    skipped = 0
    for item in items:
        parsed = parse_release(item["title"], titles)
        if not match_title(parsed, mag.title_spec()).accepted:
            skipped += 1
            continue
        ident = identify(parsed, mag)
        if not ident.confident or ident.key in found:
            skipped += ident.key not in found
            continue
        cover = ident.fields.get("cover_date")
        fields = {
            k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in ident.fields.items()
            if k in _ISSUE_FIELDS
        }
        found[ident.key] = BackIssue(
            key=ident.key,
            label=date_label(mag, ident.fields, ident.key) or ident.key,
            cover_date=cover.isoformat() if cover else None,
            issue_number=parsed.issue,
            volume_number=parsed.volume,
            source_title=item["title"],
            identifier=item["identifier"],
            fields=fields,
        )
    ordered = sorted(found.values(), key=lambda b: (b.cover_date or "", b.key), reverse=True)
    return ordered, skipped
