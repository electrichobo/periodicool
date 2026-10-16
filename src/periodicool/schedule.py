"""Expected issues from a magazine's frequency and numbering.

Every regular cadence is a sequence of slots counted from an anchor: slot 0 is
the anchor issue, slot k is k steps later (k may be negative). Issue and volume
numbers step with the slots. Irregular magazines have no slots; their issues
only come from releases and the library.

Thirteen-per-year magazines (Fortean Times) are named by cover month, with an
issue number only sometimes. They are keyed by month, generated monthly without
numbers, and the extra issue in a year is found rather than generated.

Nothing here reads the clock: callers pass `today`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool.models import Frequency, Issue, IssueStatus, Magazine, MonitorMode, Numbering
from periodicool.parser import ParsedRelease

SEASON_MONTHS = {"spring": 3, "summer": 6, "autumn": 9, "winter": 12}
_MONTH_SEASON = {m: s for s, m in SEASON_MONTHS.items()}

_MONTH_STEPS = {
    Frequency.MONTHLY: 1,
    Frequency.THIRTEEN_PER_YEAR: 1,
    Frequency.BIMONTHLY: 2,
    Frequency.QUARTERLY: 3,
    Frequency.SEASONAL: 3,
}
_DAY_STEPS = {
    Frequency.WEEKLY: 7,
    Frequency.BIWEEKLY: 14,
}
DEFAULT_GRACE_DAYS = 14
# Safety cap so a bad config (weekly since 1900) cannot flood the database.
MAX_GENERATED = 3000
# How far a weekly's name date may sit from its schedule date (on-sale vs cover).
WEEKLY_SLACK_DAYS = 3
MISSING = "missing"


@dataclass(frozen=True)
class ExpectedIssue:
    key: str
    cover_date: date
    year: int
    month: int | None = None
    month_end: int | None = None
    day: int | None = None
    season: str | None = None
    issue_number: int | None = None
    volume_number: int | None = None


@dataclass(frozen=True)
class Reference:
    """What is known about a thirteen-per-year magazine, for month estimates.

    `number` and `cover` are the newest issue known with both a number and a
    month (or the anchor); `newest` is the highest issue number known at all.
    """

    number: int
    cover: date
    newest: int


@dataclass(frozen=True)
class Identified:
    key: str | None
    fields: dict = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def confident(self) -> bool:
        return self.key is not None and not self.reasons


def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def is_regular(mag: Magazine) -> bool:
    return mag.frequency in _MONTH_STEPS or mag.frequency in _DAY_STEPS


def _anchor(mag: Magazine) -> date:
    """Slot 0, normalized to the cadence (first of month, season start, weekday)."""
    base = mag.anchor_date or mag.schedule_start
    if mag.frequency in _DAY_STEPS:
        if base:
            return base
        weekday = mag.publish_weekday or 0
        return date(2000, 1, 3) + timedelta(days=weekday)  # 2000-01-03 was a Monday
    base = base or date(2000, 1, 1)
    if mag.frequency is Frequency.SEASONAL:
        if base.month in (1, 2):
            return date(base.year - 1, 12, 1)
        return date(base.year, max(m for m in (3, 6, 9, 12) if m <= base.month), 1)
    return date(base.year, base.month, 1)


def slot_date(mag: Magazine, k: int) -> date:
    anchor = _anchor(mag)
    if mag.frequency in _MONTH_STEPS:
        return _add_months(anchor, k * _MONTH_STEPS[mag.frequency])
    return anchor + timedelta(days=k * _DAY_STEPS[mag.frequency])


def nearest_slot(mag: Magazine, d: date) -> int:
    """Index of the slot closest to d (for months: the slot that d's month falls in)."""
    anchor = _anchor(mag)
    if mag.frequency in _MONTH_STEPS:
        return _months_between(anchor, d) // _MONTH_STEPS[mag.frequency]
    return round((d - anchor).days / _DAY_STEPS[mag.frequency])


def slot_number(mag: Magazine, k: int) -> int | None:
    if mag.frequency is Frequency.THIRTEEN_PER_YEAR:
        return None  # numbers drift against months; see the module docstring
    if mag.numbering in (Numbering.ISSUE, Numbering.VOLUME) and mag.anchor_number is not None:
        return mag.anchor_number + k
    return None


def _date_key(mag: Magazine, d: date) -> str:
    if mag.frequency is Frequency.SEASONAL:
        return f"{d.year}-{_MONTH_SEASON[d.month]}"
    if mag.frequency in _DAY_STEPS:
        return d.isoformat()
    return f"{d.year}-{d.month:02d}"


def _number_key(mag: Magazine, number: int) -> str:
    return f"{'v' if mag.numbering is Numbering.VOLUME else 'n'}{number}"


def expected_for_slot(mag: Magazine, k: int) -> ExpectedIssue:
    d = slot_date(mag, k)
    number = slot_number(mag, k)
    fields: dict = {"cover_date": d, "year": d.year}
    if mag.frequency is Frequency.SEASONAL:
        fields["season"] = _MONTH_SEASON[d.month]
    else:
        fields["month"] = d.month
    if mag.frequency is Frequency.BIMONTHLY:
        fields["month_end"] = d.month % 12 + 1
    if mag.frequency in (Frequency.WEEKLY, Frequency.BIWEEKLY):
        fields["day"] = d.day
    if number is not None:
        fields["volume_number" if mag.numbering is Numbering.VOLUME else "issue_number"] = number
        key = _number_key(mag, number)
    else:
        key = _date_key(mag, d)
    return ExpectedIssue(key=key, **fields)


def expected_issues(mag: Magazine, start: date, end: date) -> list[ExpectedIssue]:
    """Expected issues with cover dates in [start, end]."""
    if not is_regular(mag) or start > end:
        return []
    k = nearest_slot(mag, start) - 1
    out: list[ExpectedIssue] = []
    while len(out) < MAX_GENERATED:
        d = slot_date(mag, k)
        if d > end:
            break
        if d >= start:
            out.append(expected_for_slot(mag, k))
        k += 1
    return out


def generation_range(mag: Magazine, today: date) -> tuple[date, date] | None:
    """Cover-date range to generate: monitor start (bounded by schedule_start) to the next issue."""
    if not is_regular(mag):
        return None
    current = slot_date(mag, nearest_slot(mag, today))
    if current > today:
        current = slot_date(mag, nearest_slot(mag, today) - 1)
    mode = mag.monitor_mode or MonitorMode.NEW_ONLY
    if mode is MonitorMode.FROM_YEAR and mag.monitor_from_year:
        start = date(mag.monitor_from_year, 1, 1)
    elif mode is MonitorMode.ALL:
        start = mag.schedule_start or _anchor(mag)
    else:
        start = current
    if mag.schedule_start:
        start = max(start, mag.schedule_start)
    end = slot_date(mag, nearest_slot(mag, current) + 1)
    return start, end


def sync_expected_issues(session: Session, mag: Magazine, today: date) -> list[Issue]:
    """Insert expected issues that do not exist yet. Never changes or deletes rows."""
    span = generation_range(mag, today)
    if span is None:
        return []
    existing = set(session.scalars(select(Issue.key).where(Issue.magazine_id == mag.id)))
    created = []
    for exp in expected_issues(mag, *span):
        if exp.key in existing:
            continue
        issue = Issue(
            magazine_id=mag.id,
            expected=True,
            monitored=mag.monitored if mag.monitored is not None else True,
            status=IssueStatus.WANTED,
            special=False,
            **vars(exp),
        )
        session.add(issue)
        existing.add(exp.key)
        created.append(issue)
    session.flush()
    return created


def effective_status(issue: Issue, mag: Magazine, today: date) -> str:
    """Stored status, or "missing" for a monitored wanted issue past its grace window."""
    grace = mag.grace_days if mag.grace_days is not None else DEFAULT_GRACE_DAYS
    if (
        issue.status is IssueStatus.WANTED
        and issue.monitored
        and mag.monitored is not False
        and not issue.file_path
        and issue.cover_date is not None
        and today > issue.cover_date + timedelta(days=grace)
    ):
        return MISSING
    return issue.status.value


# Release to issue


def _release_date(parsed: ParsedRelease) -> date | None:
    """A representative date for a parsed name (mid-month when there is no day)."""
    if parsed.year is None:
        return None
    if parsed.month:
        return date(parsed.year, parsed.month, parsed.day or 15)
    if parsed.season:
        return date(parsed.year, SEASON_MONTHS[parsed.season], 15)
    return None


def _year_numbers(mag: Magazine, year: int) -> tuple[int, int] | None:
    lo, hi = nearest_slot(mag, date(year, 1, 1)), nearest_slot(mag, date(year, 12, 31))
    a, b = slot_number(mag, lo), slot_number(mag, hi)
    return None if a is None or b is None else (a, b)


def _covered(mag: Magazine, d: date) -> bool:
    return is_regular(mag) and (mag.schedule_start is None or d >= mag.schedule_start)


def identify(
    parsed: ParsedRelease, mag: Magazine, reference: Reference | None = None
) -> Identified:
    """Map a parsed release (already matched to this magazine) to an issue key.

    With a `reference`, a thirteen-per-year name with a number but no month
    gets an estimated month (see `_estimate_month`). Without one it is flagged.
    """
    reasons = list(parsed.reasons)
    notes: list[str] = []
    fields: dict = {
        "year": parsed.year,
        "month": parsed.month,
        "month_end": parsed.month_end,
        "day": parsed.day,
        "season": parsed.season,
        "special": parsed.special,
    }
    rel_date = _release_date(parsed)

    if parsed.special:
        if parsed.year and parsed.month:
            d = date(parsed.year, parsed.month, parsed.day or 1)
            key = f"special-{d.isoformat() if parsed.day else d.isoformat()[:7]}"
            return Identified(key, fields | {"cover_date": d}, tuple(reasons))
        return Identified(None, fields, (*reasons, "special without a month"))

    if mag.numbering in (Numbering.ISSUE, Numbering.VOLUME):
        return _identify_numbered(parsed, mag, fields, rel_date, reasons, notes, reference)
    return _identify_dated(parsed, mag, fields, rel_date, reasons, notes)


def _identify_numbered(parsed, mag, fields, rel_date, reasons, notes, reference=None) -> Identified:
    volume = mag.numbering is Numbering.VOLUME
    number = (parsed.volume or parsed.issue) if volume else parsed.issue
    if mag.frequency is Frequency.THIRTEEN_PER_YEAR:
        return _identify_thirteen(parsed, mag, fields, number, reasons, reference)
    numbered = mag.anchor_number is not None and is_regular(mag)
    tolerance = 2 if mag.frequency in _DAY_STEPS else 1

    if number is None:
        if rel_date and numbered and mag.frequency in _MONTH_STEPS and _covered(mag, rel_date):
            number = slot_number(mag, nearest_slot(mag, rel_date))
            notes.append(f"number {number} taken from the schedule")
        else:
            reasons.append("no issue number")
            return Identified(None, fields, tuple(reasons), tuple(notes))
    elif numbered and rel_date and _covered(mag, rel_date):
        guess = slot_number(mag, nearest_slot(mag, rel_date))
        if guess is not None and abs(number - guess) > tolerance:
            reasons.append(f"number {number} does not fit {rel_date:%Y-%m}, expected about {guess}")
    elif numbered and parsed.year and _covered(mag, date(parsed.year, 12, 31)):
        span = _year_numbers(mag, parsed.year)
        if span and not span[0] - tolerance <= number <= span[1] + tolerance:
            reasons.append(
                f"number {number} does not fit {parsed.year}, expected {span[0]} to {span[1]}"
            )

    fields["volume_number" if volume else "issue_number"] = number
    if rel_date:
        fields["cover_date"] = rel_date.replace(day=parsed.day or 1)
    elif numbered:
        k = number - mag.anchor_number
        fields["cover_date"] = slot_date(mag, k)
    return Identified(_number_key(mag, number), fields, tuple(reasons), tuple(notes))


def _estimate_month(number: int, ref: Reference) -> date:
    """Cover month for a newer issue number, at 13 issues a year from the reference."""
    months = round((number - ref.number) * 12 / 13)
    return _add_months(date(ref.cover.year, ref.cover.month, 1), months)


def _identify_thirteen(parsed, mag, fields, number, reasons, reference=None) -> Identified:
    """Key by cover month; keep the number when the name has one."""
    if not (parsed.year and parsed.month):
        if number is None:
            reasons.append("no month")
            return Identified(None, fields, tuple(reasons))
        if reference is None:
            reasons.append("number without a month")
            return Identified(None, fields, tuple(reasons))
        if number <= reference.newest:
            reasons.append(
                f"number {number} without a month is not newer than issue {reference.newest}"
            )
            return Identified(None, fields, tuple(reasons))
        d = _estimate_month(number, reference)
        if parsed.year and parsed.year != d.year:
            reasons.append(f"estimated month {d:%Y-%m} for number {number} is not in {parsed.year}")
            return Identified(None, fields, tuple(reasons))
        note = f"month {d:%Y-%m} estimated from issue {number} (13 a year from {reference.number})"
        fields |= {
            "year": d.year,
            "month": d.month,
            "day": None,
            "cover_date": d,
            "issue_number": number,
            "estimated": True,
        }
        return Identified(f"{d.year}-{d.month:02d}", fields, tuple(reasons), (note,))
    d = date(parsed.year, parsed.month, 1)
    if number is not None and mag.anchor_number is not None and mag.anchor_date:
        months = _months_between(_anchor(mag), d)
        guess = mag.anchor_number + round(months * 13 / 12)
        if abs(number - guess) > 2:
            reasons.append(f"number {number} does not fit {d:%Y-%m}, expected about {guess}")
    fields |= {"day": None, "cover_date": d, "issue_number": number}
    return Identified(f"{d.year}-{d.month:02d}", fields, tuple(reasons))


def _identify_dated(parsed, mag, fields, rel_date, reasons, notes) -> Identified:
    freq = mag.frequency
    if freq is Frequency.SEASONAL:
        if parsed.season and parsed.year:
            d = date(parsed.year, SEASON_MONTHS[parsed.season], 1)
            return Identified(_date_key(mag, d), fields | {"cover_date": d}, tuple(reasons))
        reasons.append("no season")
        return Identified(None, fields, tuple(reasons))

    if freq in (Frequency.WEEKLY, Frequency.BIWEEKLY):
        if not (parsed.year and parsed.month and parsed.day):
            reasons.append("no day for a weekly")
            return Identified(None, fields, tuple(reasons))
        d = date(parsed.year, parsed.month, parsed.day)
        slot = slot_date(mag, nearest_slot(mag, d))
        off = abs((slot - d).days)
        if off > WEEKLY_SLACK_DAYS:
            reasons.append(f"{d} is {off} days from the schedule")
            slot = d
        elif off:
            notes.append(f"{d} moved to schedule date {slot}")
        fields |= {"year": slot.year, "month": slot.month, "day": slot.day, "cover_date": slot}
        return Identified(_date_key(mag, slot), fields, tuple(reasons), tuple(notes))

    if not (parsed.year and parsed.month):
        reasons.append("no month")
        return Identified(None, fields, tuple(reasons))
    d = date(parsed.year, parsed.month, 1)
    if freq in (Frequency.BIMONTHLY, Frequency.QUARTERLY) and _covered(mag, d):
        slot = slot_date(mag, nearest_slot(mag, d))
        if slot != d:
            reasons.append(f"{d:%Y-%m} does not start a {freq.value} issue")
    if freq is Frequency.IRREGULAR and parsed.day:
        d = date(parsed.year, parsed.month, parsed.day)
        return Identified(d.isoformat(), fields | {"cover_date": d}, tuple(reasons))
    fields |= {"day": None, "cover_date": d}
    if parsed.day:
        notes.append(f"day {parsed.day} ignored for a {freq.value} magazine")
    return Identified(f"{d.year}-{d.month:02d}", fields, tuple(reasons), tuple(notes))
