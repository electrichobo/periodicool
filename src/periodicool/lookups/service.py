"""Cached lookups. Nothing here touches the network unless the source is turned on."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from periodicool.lookups import SOURCES, LookupError, Record, enabled
from periodicool.models import LookupCache, Magazine

log = logging.getLogger(__name__)

FRESH = timedelta(days=30)
FAILED_FRESH = timedelta(days=1)
UNUSED = timedelta(days=180)
# Quarterly and seasonal are the same cadence for this check.
_SAME = {"seasonal": "quarterly"}


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _query(issn: str | None, title: str | None) -> str:
    return f"issn:{issn}" if issn else f"title:{(title or '').strip().lower()}"


def _row(session: Session, source: str, query: str) -> LookupCache | None:
    return session.scalars(
        select(LookupCache).where(LookupCache.source == source, LookupCache.query == query)
    ).one_or_none()


def _fetch(source: str, issn, title, transport):
    from periodicool.lookups import archive, loc, wikidata

    if source == "loc":
        rec = loc.lookup(issn=issn, title=title, transport=transport)
        return rec.as_dict() if rec else None
    if source == "wikidata":
        rec = wikidata.lookup(issn=issn, title=title, transport=transport)
        return rec.as_dict() if rec else None
    if source == "archive":
        return {"items": archive.search(title or "", transport=transport)}
    raise ValueError(source)


@dataclass
class Answer:
    source: str
    data: dict | None = None
    error: str | None = None
    off: bool = False
    cached: bool = False

    @property
    def record(self) -> Record | None:
        if self.source == "archive" or not self.data:
            return None
        return Record.from_dict(self.data)


def lookup(
    session: Session,
    source: str,
    *,
    issn: str | None = None,
    title: str | None = None,
    transport=None,
    force: bool = False,
) -> Answer:
    """One source's answer for an ISSN (preferred) or a title, from the cache when fresh."""
    if not enabled(session, source):
        return Answer(source, off=True)
    if source == "archive":
        issn = None  # the Internet Archive has no usable ISSN search
    if not issn and not title:
        return Answer(source, error="nothing to look up")
    query = _query(issn, title)
    row = _row(session, source, query)
    now = _now()
    if row is not None and not force:
        age = now - _aware(row.fetched_at)
        if age < (FAILED_FRESH if row.error else FRESH):
            row.used_at = now
            return Answer(source, row.data, row.error, cached=True)
    try:
        data, error = _fetch(source, issn, title, transport), None
    except LookupError as exc:
        data, error = None, str(exc)[:300]
        log.info("%s lookup failed: %s", SOURCES[source], error)
    if row is None:
        row = LookupCache(source=source, query=query)
        session.add(row)
    row.data, row.error, row.fetched_at, row.used_at = data, error, now, now
    session.flush()
    return Answer(source, data, error)


def records(session: Session, mag: Magazine, transport=None, force: bool = False) -> dict:
    """LoC and Wikidata answers for a magazine: by ISSN when set, else by title."""
    return {
        s: lookup(session, s, issn=mag.issn, title=mag.title, transport=transport, force=force)
        for s in ("loc", "wikidata")
    }


def cached_records(session: Session, mag: Magazine) -> list[Record]:
    """Records already in the cache for a magazine's ISSN. Never touches the network."""
    if not mag.issn:
        return []
    out = []
    for source in ("loc", "wikidata"):
        if not enabled(session, source):
            continue
        row = _row(session, source, _query(mag.issn, None))
        if row is not None and row.data:
            out.append(Record.from_dict(row.data))
    return out


def extra_names(session: Session, magazines) -> dict[int, list[tuple[str, str]]]:
    """Alternate titles from cached records, per magazine id, as (name, source label).

    Library side only: these help recognize files, never releases.
    """
    from periodicool.matching import normalize_title

    out: dict[int, list[tuple[str, str]]] = {}
    for mag in magazines:
        own = {normalize_title(t) for t in (mag.title, *(mag.aliases or []))}
        names = []
        for rec in cached_records(session, mag):
            for name in [rec.title or "", *rec.alt_titles]:
                key = normalize_title(name)
                if key and key not in own and len(key) >= 3:
                    own.add(key)
                    names.append((name, SOURCES[rec.source]))
        if names:
            out[mag.id] = names
    return out


def frequency_mismatch(session: Session, mag: Magazine) -> str | None:
    """A note when a cached record's frequency disagrees with the magazine's."""
    ours = mag.frequency.value
    for rec in cached_records(session, mag):
        theirs = rec.frequency
        if theirs and _SAME.get(theirs, theirs) != _SAME.get(ours, ours):
            return (
                f"{SOURCES[rec.source]} says {rec.frequency_text}; "
                f"{mag.title} is set to {ours.replace('_', ' ')}"
            )
    return None


def prune(session: Session) -> int:
    cutoff = _now() - UNUSED
    return session.execute(delete(LookupCache).where(LookupCache.used_at < cutoff)).rowcount


def candidates(
    session: Session,
    source: str,
    *,
    issn: str | None = None,
    title: str | None = None,
    transport=None,
) -> tuple[list[Record], str | None]:
    """Several possible matches from one source (for the add search), cached like
    lookups. Returns ([], None) when the source is off."""
    from periodicool.lookups import loc, wikidata

    if source not in ("loc", "wikidata") or not enabled(session, source):
        return [], None
    if not issn and not title:
        return [], None
    query = "list|" + _query(issn, title)
    row = _row(session, source, query)
    now = _now()
    if row is not None:
        age = now - _aware(row.fetched_at)
        if age < (FAILED_FRESH if row.error else FRESH):
            row.used_at = now
            found = [Record.from_dict(d) for d in (row.data or {}).get("records", [])]
            return found, row.error
    module = loc if source == "loc" else wikidata
    try:
        found, error = module.candidates(issn=issn, title=title, transport=transport), None
    except LookupError as exc:
        found, error = [], str(exc)[:300]
        log.info("%s search failed: %s", SOURCES[source], error)
    if row is None:
        row = LookupCache(source=source, query=query)
        session.add(row)
    row.data = {"records": [r.as_dict() for r in found]}
    row.error, row.fetched_at, row.used_at = error, now, now
    session.flush()
    return found, error


def remember(session: Session, mag: Magazine, records: list[Record]) -> None:
    """Store records picked while adding as this magazine's lookup answers, so its
    Metadata pop-up shows them at once instead of asking again."""
    now = _now()
    for rec in records:
        if rec.source not in ("loc", "wikidata"):
            continue
        query = _query(mag.issn, mag.title)
        row = _row(session, rec.source, query)
        if row is None:
            row = LookupCache(source=rec.source, query=query)
            session.add(row)
        row.data, row.error, row.fetched_at, row.used_at = rec.as_dict(), None, now, now
    session.flush()
