"""The Add magazine search: likely magazines from release names and from the opt-in
metadata sources, merged into one list of cards.

Release names are the only source that always exists. They are parsed, grouped by
the title the parser finds, and each group gets guesses for numbering, frequency
and the newest issue. Guesses are suggestions shown in the add form, never saved
without the user.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from periodicool.lookups import Record
from periodicool.models import Frequency, Numbering
from periodicool.parser import ParsedRelease, parse_release
from periodicool.schedule import SEASON_MONTHS

_FILLER = {"the", "magazine", "mag", "uk", "us", "usa"}
MIN_RELEASES = 2  # a single stray name is not a magazine


def title_key(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    kept = [w for w in words if w not in _FILLER]
    return " ".join(kept or words)


@dataclass
class Guess:
    numbering: Numbering | None = None
    frequency: Frequency | None = None
    anchor_date: date | None = None
    anchor_number: int | None = None
    region: str | None = None
    language: str | None = None


@dataclass
class Card:
    title: str
    key: str
    releases: int = 0
    examples: list[str] = field(default_factory=list)
    guess: Guess = field(default_factory=Guess)
    records: list[Record] = field(default_factory=list)  # metadata, best first
    tracked_id: int | None = None  # already a tracked magazine

    @property
    def sources(self) -> list[str]:
        return list(dict.fromkeys(r.source for r in self.records))

    @property
    def issns(self) -> list[str]:
        return list(dict.fromkeys(code for r in self.records for code in r.issns))

    @property
    def alt_titles(self) -> list[str]:
        names = [a for r in self.records for a in r.alt_titles]
        names += [r.title for r in self.records if r.title and title_key(r.title) != self.key]
        return [n for n in dict.fromkeys(names) if n and n != self.title]

    def first(self, attr: str):
        return next((getattr(r, attr) for r in self.records if getattr(r, attr)), None)

    @property
    def frequency(self) -> str | None:
        """The catalog's frequency when it says one, else the guess from releases."""
        return self.first("frequency") or (
            self.guess.frequency.value if self.guess.frequency else None
        )

    @property
    def frequency_from(self) -> str:
        if self.first("frequency"):
            return "catalog"
        return "release names" if self.guess.frequency else ""


def _issue_date(p: ParsedRelease) -> date | None:
    if p.year is None:
        return None
    try:
        if p.week:
            return date.fromisocalendar(p.year, p.week, 1)
        if p.season:
            return date(p.year, SEASON_MONTHS.get(p.season, 1), 1)
        if p.month:
            return date(p.year, p.month, p.day or 1)
    except ValueError:
        return None
    return None


def guess(parsed: list[ParsedRelease]) -> Guess:
    out = Guess()
    if not parsed:
        return out
    n = len(parsed)

    def share(attr: str) -> float:
        return sum(1 for p in parsed if getattr(p, attr)) / n

    if share("issue") >= 0.5:
        out.numbering = Numbering.ISSUE
    elif share("volume") >= 0.5:
        out.numbering = Numbering.VOLUME
    elif share("season") >= 0.5:
        out.numbering = Numbering.SEASON
    elif any(_issue_date(p) for p in parsed):
        out.numbering = Numbering.DATE

    dated = sorted({d for p in parsed if (d := _issue_date(p))})
    if out.numbering is Numbering.SEASON or (dated and share("season") >= 0.5):
        out.frequency = Frequency.SEASONAL
    elif len(dated) >= 3:
        gaps = [(b - a).days for a, b in zip(dated, dated[1:], strict=False)]
        gap = statistics.median(gaps)
        numbered = [(p.issue, d) for p in parsed if p.issue and (d := _issue_date(p))]
        per_year = None
        if len(numbered) >= 3:
            (n1, d1), (n2, d2) = (
                min(numbered, key=lambda x: x[1]),
                max(numbered, key=lambda x: x[1]),
            )
            if d2 > d1 and n2 > n1:
                per_year = (n2 - n1) / ((d2 - d1).days / 365.25)
        if gap <= 10:
            out.frequency = Frequency.WEEKLY
        elif gap <= 20:
            out.frequency = Frequency.BIWEEKLY
        elif gap <= 40:
            thirteen = per_year is not None and per_year >= 12.5
            out.frequency = Frequency.THIRTEEN_PER_YEAR if thirteen else Frequency.MONTHLY
        elif gap <= 75:
            out.frequency = Frequency.BIMONTHLY
        elif gap <= 120:
            out.frequency = Frequency.QUARTERLY
        else:
            out.frequency = Frequency.IRREGULAR
    elif out.numbering is Numbering.VOLUME:
        out.frequency = Frequency.IRREGULAR

    newest = max((p for p in parsed if _issue_date(p)), key=lambda p: _issue_date(p), default=None)
    if newest is not None:
        out.anchor_date = _issue_date(newest)
        number = newest.volume if out.numbering is Numbering.VOLUME else newest.issue
        if out.numbering in (Numbering.ISSUE, Numbering.VOLUME) and number:
            out.anchor_number = number
    regions = {p.region for p in parsed}
    if len(regions) == 1 and None not in regions:
        out.region = regions.pop()
    languages = {p.language for p in parsed}
    if len(languages) == 1 and None not in languages:
        out.language = languages.pop()
    return out


def from_releases(names: list[str], dates: dict[str, datetime] | None = None) -> list[Card]:
    """Group release names into likely magazines, most releases first."""
    groups: dict[str, list[ParsedRelease]] = {}
    for name in names:
        p = parse_release(name)
        if not p.title or p.pack:
            continue
        key = title_key(p.title)
        if key:
            groups.setdefault(key, []).append(p)
    cards = []
    for key, parsed in groups.items():
        if len(parsed) < MIN_RELEASES:
            continue
        title = Counter(p.title for p in parsed).most_common(1)[0][0]
        newest = sorted(parsed, key=lambda p: _issue_date(p) or date.min, reverse=True)
        cards.append(
            Card(
                title=title,
                key=key,
                releases=len(parsed),
                examples=[p.raw for p in newest[:3]],
                guess=guess(parsed),
            )
        )
    cards.sort(key=lambda c: -c.releases)
    return cards


def merge(
    term: str, records: list[Record], cards: list[Card], tracked: dict[str, int]
) -> list[Card]:
    """Metadata records join the release card with the same title (or each other, by
    ISSN); tracked magazines are flagged. Cards whose title matches the search come
    first, then metadata cards, then the rest by release count."""
    by_key = {c.key: c for c in cards}
    merged: list[Card] = list(cards)
    for rec in records:
        key = title_key(rec.title or "")
        target = by_key.get(key)
        if target is None:
            target = next((c for c in merged if set(rec.issns) & set(c.issns)), None)
        if target is None:
            target = Card(title=rec.title or "", key=key)
            by_key[key] = target
            merged.append(target)
        target.records.append(rec)
    want = title_key(term)
    for c in merged:
        c.tracked_id = tracked.get(c.key)
        if c.records and c.records[0].title and not c.releases:
            c.title = c.records[0].title

    def order(c: Card):
        exact = c.key == want or want in c.key
        return (not exact, not c.records, -c.releases)

    return sorted(merged, key=order)
