"""Read-only data for Activity, Wanted and Calendar, plus the iCal feed text."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from periodicool.models import (
    ACTIVE_GRAB,
    Grab,
    GrabStatus,
    History,
    HistoryEvent,
    Issue,
    Magazine,
    SearchState,
)
from periodicool.overview import STATES, issue_label, issue_state

PAGE_SIZE = 50
QUEUE_STATUSES = (*ACTIVE_GRAB, GrabStatus.REVIEW)
GRAB_LEDS = {
    GrabStatus.QUEUED: ("off", "Queued"),
    GrabStatus.DOWNLOADING: ("info", "Downloading"),
    GrabStatus.COMPLETED: ("info", "Importing"),
    GrabStatus.REVIEW: ("warn", "Needs review"),
}


@dataclass
class Page:
    rows: list
    page: int
    pages: int
    total: int
    states: list = field(default_factory=list)  # (value, label) for a filter menu


def _page(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int, int]:
    pages = max(1, -(-total // size))
    page = min(max(page, 1), pages)
    return page, pages, (page - 1) * size


# Queue


def queue(session: Session) -> list[Grab]:
    return list(
        session.scalars(
            select(Grab)
            .where(Grab.status.in_(QUEUE_STATUSES))
            .options(selectinload(Grab.magazine), selectinload(Grab.release))
            .order_by(Grab.grabbed_at)
        )
    )


def has_active(session: Session) -> bool:
    count = session.scalar(
        select(func.count()).select_from(Grab).where(Grab.status.in_(ACTIVE_GRAB))
    )
    return bool(count)


def format_seconds(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds} s"
    minutes, s = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{s:02d}" if hours else f"{minutes}:{s:02d}"


# History


def history_page(
    session: Session, page: int = 1, event: str | None = None, magazine_id: int | None = None
) -> Page:
    query = select(History)
    if event:
        query = query.where(History.event == HistoryEvent(event))
    if magazine_id:
        query = query.where(History.magazine_id == magazine_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    page, pages, offset = _page(total, page)
    rows = list(
        session.scalars(
            query.options(selectinload(History.magazine))
            .order_by(History.date.desc(), History.id.desc())
            .offset(offset)
            .limit(PAGE_SIZE)
        )
    )
    return Page(rows, page, pages, total)


# Wanted


@dataclass
class WantedRow:
    issue: Issue
    mag: Magazine
    state: str
    label: str
    last_search: datetime | None

    @property
    def led(self) -> str:
        return STATES[self.state][0]

    @property
    def state_label(self) -> str:
        return STATES[self.state][1]


def _wanted_query():
    return (
        select(Issue)
        .join(Magazine)
        .where(
            Magazine.monitored.is_(True),
            Issue.monitored.is_(True),
            Issue.file_path.is_(None),
        )
        .options(selectinload(Issue.magazine))
        .order_by(Issue.cover_date.desc().nulls_last(), Magazine.title)
    )


def _wanted_filter(mag: Magazine, text: str, magazine_id: int | None) -> bool:
    if magazine_id is not None and mag.id != magazine_id:
        return False
    if text:
        names = [mag.title, *(mag.aliases or [])]
        return any(text in name.lower() for name in names)
    return True


def missing(
    session: Session,
    today: date,
    page: int = 1,
    *,
    text: str = "",
    magazine_id: int | None = None,
    state: str = "",
) -> Page:
    """Monitored issues without a file that are out (or due), like Sonarr's Missing.
    Filters: words in the magazine's title or other names, one magazine, one state.
    The page's `states` lists the states present before filtering, for the filter menu."""
    searched = {s.magazine_id: s.last_search_at for s in session.scalars(select(SearchState))}
    text = text.strip().lower()
    rows, states = [], {}
    for issue in session.scalars(_wanted_query()):
        found = issue_state(issue, issue.magazine, today)
        if found in ("upcoming", "unmonitored", "grabbed"):
            continue
        row = WantedRow(
            issue,
            issue.magazine,
            found,
            issue_label(issue, issue.magazine),
            searched.get(issue.magazine_id),
        )
        states.setdefault(found, row.state_label)
        if state and found != state:
            continue
        if not _wanted_filter(issue.magazine, text, magazine_id):
            continue
        rows.append(row)
    page, pages, offset = _page(len(rows), page)
    result = Page(rows[offset : offset + PAGE_SIZE], page, pages, len(rows))
    result.states = sorted(states.items(), key=lambda kv: kv[1])
    return result


@dataclass
class CutoffRow:
    issue: Issue
    mag: Magazine
    label: str
    wanted_format: str


def cutoff_unmet(
    session: Session, page: int = 1, *, text: str = "", magazine_id: int | None = None
) -> Page:
    """Imported issues whose format is below the profile's first choice."""
    text = text.strip().lower()
    rows = []
    query = (
        select(Issue)
        .join(Magazine)
        .where(Issue.file_path.is_not(None), Magazine.format_profile_id.is_not(None))
        .options(selectinload(Issue.magazine).selectinload(Magazine.format_profile))
        .order_by(Magazine.title, Issue.cover_date.desc().nulls_last())
    )
    for issue in session.scalars(query):
        profile = issue.magazine.format_profile
        best = profile.formats[0] if profile and profile.formats else None
        fmt = (issue.file_format or "").lower()
        if not _wanted_filter(issue.magazine, text, magazine_id):
            continue
        if best and fmt and fmt != best:
            rows.append(CutoffRow(issue, issue.magazine, issue_label(issue, issue.magazine), best))
    page, pages, offset = _page(len(rows), page)
    return Page(rows[offset : offset + PAGE_SIZE], page, pages, len(rows))


# Calendar


@dataclass
class CalendarEntry:
    issue: Issue
    mag: Magazine
    state: str
    label: str

    @property
    def on_sale(self) -> date | None:
        if self.mag.on_sale_days is None or self.issue.cover_date is None:
            return None
        return self.issue.cover_date - timedelta(days=self.mag.on_sale_days)

    @property
    def led(self) -> str:
        return STATES[self.state][0]

    @property
    def state_label(self) -> str:
        return STATES[self.state][1]


def entries(session: Session, start: date, end: date, today: date) -> dict[date, list]:
    """Issues by cover date in [start, end], for monitored and unmonitored magazines."""
    query = (
        select(Issue)
        .where(Issue.cover_date >= start, Issue.cover_date <= end)
        .options(selectinload(Issue.magazine))
        .order_by(Issue.cover_date)
    )
    out: dict[date, list] = {}
    for issue in session.scalars(query):
        mag = issue.magazine
        out.setdefault(issue.cover_date, []).append(
            CalendarEntry(issue, mag, issue_state(issue, mag, today), issue_label(issue, mag))
        )
    for day in out.values():
        day.sort(key=lambda e: e.mag.title.lower())
    return out


def month_grid(year: int, month: int, first_weekday: int = 0) -> list[list[date]]:
    """Weeks of the month view, padded with days of the months around it."""
    return calendar.Calendar(first_weekday).monthdatescalendar(year, month)


def week_days(day: date, first_weekday: int = 0) -> list[date]:
    start = day - timedelta(days=(day.weekday() - first_weekday) % 7)
    return [start + timedelta(days=i) for i in range(7)]


# iCal

FEED_PAST_DAYS = 30
FEED_FUTURE_DAYS = 90


def _ical_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold lines at 75 octets, as RFC 5545 asks."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += b
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def ical_feed(session: Session, today: date, host: str = "periodicool") -> str:
    start, end = today - timedelta(days=FEED_PAST_DAYS), today + timedelta(days=FEED_FUTURE_DAYS)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PeriodiCool//Magazine calendar//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:PeriodiCool",
    ]
    for day, items in sorted(entries(session, start, end, today).items()):
        for e in items:
            if e.mag.monitored is False:
                continue
            lines += [
                "BEGIN:VEVENT",
                f"UID:issue-{e.issue.id}@{host}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
                f"SUMMARY:{_ical_text(f'{e.mag.title} {e.label}')}",
                "DESCRIPTION:"
                + _ical_text(
                    e.state_label
                    + (f". On sale about {e.on_sale.isoformat()}" if e.on_sale else "")
                ),
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
