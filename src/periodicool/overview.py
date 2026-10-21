"""Read-only summaries for the magazine screens: counts, sizes, latest and next issue,
issues grouped by year, and chart rows. No business rules live in the web routes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from periodicool.library import TEMPLATE_KEY, get_setting
from periodicool.models import Issue, IssueStatus, Magazine
from periodicool.naming import DEFAULT_TEMPLATE, UnsafePathError, date_label, render
from periodicool.schedule import MISSING, effective_status

# Display state for an issue: (key, LED kind, label). Every state has its own label,
# so status never depends on color alone.
STATES = {
    "imported": ("ok", "Imported"),
    "downloaded": ("ok", "Downloaded"),
    "grabbed": ("info", "Downloading"),
    "wanted": ("warn", "Wanted"),
    MISSING: ("bad", "Missing"),
    "failed": ("bad", "Failed"),
    "upcoming": ("off", "Upcoming"),
    "unmonitored": ("off", "Unmonitored"),
}

EVENT_LABELS = {
    "grabbed": "Grabbed",
    "download_failed": "Download failed",
    "download_removed": "Removed",
    "imported": "Imported",
    "import_failed": "Import failed",
    "import_review": "Needs review",
    "file_moved": "File moved",
    "file_linked": "File linked",
    "blocklisted": "Blocklisted",
}
EVENT_LEDS = {
    "grabbed": "info",
    "download_failed": "bad",
    "download_removed": "off",
    "imported": "ok",
    "import_failed": "bad",
    "import_review": "warn",
    "file_moved": "ok",
    "file_linked": "ok",
    "blocklisted": "bad",
}

FREQUENCY_LABELS = {
    "weekly": "Weekly",
    "biweekly": "Every two weeks",
    "monthly": "Monthly",
    "thirteen_per_year": "13 / year",
    "bimonthly": "Bimonthly",
    "quarterly": "Quarterly",
    "seasonal": "Seasonal",
    "irregular": "Irregular",
}
NUMBERING_LABELS = {"date": "Date", "issue": "Issue number", "volume": "Volume", "season": "Season"}


def issue_state(issue: Issue, mag: Magazine, today: date) -> str:
    if issue.file_path:
        return "downloaded" if issue.status is IssueStatus.DOWNLOADED else "imported"
    if not issue.monitored or mag.monitored is False:
        return "unmonitored"
    if issue.status is IssueStatus.GRABBED:
        return "grabbed"
    if issue.cover_date is not None and issue.cover_date > today:
        return "upcoming"
    return effective_status(issue, mag, today)


def issue_fields(issue: Issue) -> dict:
    return {
        k: getattr(issue, k)
        for k in (
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
    }


def issue_label(issue: Issue, mag: Magazine) -> str:
    label = date_label(mag, issue_fields(issue), issue.key)
    if issue.special_title:
        label = f"{label} {issue.special_title}".strip()
    return label or issue.key


def issue_year(issue: Issue) -> int | None:
    if issue.cover_date is not None:
        return issue.cover_date.year
    return issue.year


def magazine_folder(session: Session, mag: Magazine) -> str | None:
    """The magazine's top folder in the library, from the naming template."""
    template = get_setting(session, TEMPLATE_KEY, DEFAULT_TEMPLATE)
    try:
        path = render(template, mag, {"year": date.today().year, "month": 1}, "pdf")
    except UnsafePathError, KeyError, ValueError:
        return None
    return path.parts[0] if len(path.parts) > 1 else None


@dataclass
class IssueRow:
    issue: Issue
    state: str
    label: str

    @property
    def led(self) -> str:
        return STATES[self.state][0]

    @property
    def state_label(self) -> str:
        return STATES[self.state][1]


@dataclass
class YearGroup:
    year: int | None
    rows: list[IssueRow] = field(default_factory=list)

    @property
    def have(self) -> int:
        return sum(r.state in ("imported", "downloaded") for r in self.rows)

    @property
    def total(self) -> int:
        return sum(r.state not in ("upcoming", "unmonitored") for r in self.rows)

    @property
    def monitored(self) -> bool:
        return any(r.issue.monitored for r in self.rows)


@dataclass
class Summary:
    mag: Magazine
    counts: Counter
    size: int
    files: int
    latest: IssueRow | None
    next: IssueRow | None
    issues: int

    @property
    def have(self) -> int:
        return self.counts["imported"] + self.counts["downloaded"]

    @property
    def total(self) -> int:
        """Monitored issues that are out (or should be), like Sonarr's aired episodes."""
        return self.have + sum(self.counts[k] for k in ("grabbed", "wanted", MISSING, "failed"))

    @property
    def missing(self) -> int:
        return self.counts[MISSING] + self.counts["failed"]

    @property
    def wanted(self) -> int:
        return self.counts["wanted"]

    @property
    def downloading(self) -> int:
        return self.counts["grabbed"]

    @property
    def percent(self) -> float:
        return self.have / self.total * 100 if self.total else (100.0 if self.have else 0.0)

    @property
    def frequency_label(self) -> str:
        return FREQUENCY_LABELS.get(self.mag.frequency.value, self.mag.frequency.value)

    @property
    def numbering_label(self) -> str:
        return NUMBERING_LABELS.get(self.mag.numbering.value, self.mag.numbering.value)


def _rows(mag: Magazine, today: date) -> list[IssueRow]:
    return [IssueRow(i, issue_state(i, mag, today), issue_label(i, mag)) for i in mag.issues]


def _sort_key(row: IssueRow):
    i = row.issue
    return (
        i.cover_date or date.min,
        i.issue_number or 0,
        i.volume_number or 0,
        i.key,
    )


def summarize(mag: Magazine, today: date, rows: list[IssueRow] | None = None) -> Summary:
    rows = _rows(mag, today) if rows is None else rows
    counts = Counter(r.state for r in rows)
    with_files = [r for r in rows if r.issue.file_path]
    latest = max(with_files, key=_sort_key, default=None)
    upcoming = [r for r in rows if r.issue.cover_date and r.issue.cover_date > today]
    nxt = min(upcoming, key=_sort_key, default=None)
    return Summary(
        mag=mag,
        counts=counts,
        size=sum(r.issue.file_size or 0 for r in with_files),
        files=len(with_files),
        latest=latest,
        next=nxt,
        issues=len(rows),
    )


def all_summaries(session: Session, today: date) -> list[Summary]:
    mags = session.scalars(
        select(Magazine).options(selectinload(Magazine.issues)).order_by(Magazine.title)
    )
    return [summarize(m, today) for m in mags]


def totals(summaries: list[Summary]) -> dict:
    return {
        "magazines": len(summaries),
        "monitored": sum(bool(s.mag.monitored) for s in summaries),
        "issues": sum(s.issues for s in summaries),
        "files": sum(s.files for s in summaries),
        "size": sum(s.size for s in summaries),
        "missing": sum(s.missing for s in summaries),
    }


def year_groups(mag: Magazine, today: date) -> tuple[Summary, list[YearGroup]]:
    rows = _rows(mag, today)
    groups: dict[int | None, YearGroup] = {}
    for row in rows:
        year = issue_year(row.issue)
        groups.setdefault(year, YearGroup(year)).rows.append(row)
    for group in groups.values():
        group.rows.sort(key=_sort_key, reverse=True)
    ordered = sorted(groups.values(), key=lambda g: (g.year is None, -(g.year or 0)))
    return summarize(mag, today, rows), ordered


def chart_rows(groups: list[YearGroup], limit: int = 10) -> list[tuple]:
    """(label, have, wanted, missing) for the newest years, oldest first."""
    out = []
    for g in [g for g in groups if g.year is not None][:limit]:
        states = Counter(r.state for r in g.rows)
        have = states["imported"] + states["downloaded"]
        wanted = states["wanted"] + states["grabbed"]
        missing = states[MISSING] + states["failed"]
        out.append((str(g.year), have, wanted, missing))
    return list(reversed(out))


def all_tags(session: Session) -> list[str]:
    tags = set()
    for value in session.scalars(select(Magazine.tags)):
        tags.update(value or [])
    return sorted(tags, key=str.lower)


def cover_file(library_root, mag: Magazine):
    """The cover image path, only if it is inside the library."""
    if not mag.cover_path:
        return None
    rel = PurePosixPath(mag.cover_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    path = library_root / rel
    try:
        path.resolve().relative_to(library_root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None
