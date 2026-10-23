"""Accept or reject a release, with every reason.

Interactive search shows these reasons for every candidate, so `evaluate`
keeps checking after the first rejection wherever later checks still make
sense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from periodicool.matching import TitleIndex, match_title
from periodicool.models import (
    FormatProfile,
    Frequency,
    Issue,
    IssueStatus,
    Magazine,
    MonitorMode,
    Release,
)
from periodicool.parser import FORMATS, ParsedRelease, parse_release
from periodicool.schedule import Reference, generation_range, identify

MIN_SIZE_KEY = "release.min_size_mb"
# 0 means no limit for these three, as in Sonarr.
MAX_SIZE_KEY = "release.max_size_mb"
MIN_AGE_KEY = "release.min_age_min"
RETENTION_KEY = "release.retention_days"
EARLY_DAYS_KEY = "search.early_days"
MUST_CONTAIN_KEY = "release.must_contain"
MUST_NOT_CONTAIN_KEY = "release.must_not_contain"
DEFAULT_MIN_SIZE_MB = 1
DEFAULT_EARLY_DAYS = 45
# Irregular magazines on "new only" accept releases published this recently.
NEW_ONLY_DAYS = 60


@dataclass(frozen=True)
class Options:
    min_size_mb: float = DEFAULT_MIN_SIZE_MB
    max_size_mb: float = 0
    min_age_min: int = 0
    retention_days: int = 0
    early_days: int = DEFAULT_EARLY_DAYS
    # Release rules for every magazine, on top of each magazine's own.
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()


def load_options(session: Session) -> Options:
    from periodicool.library import get_setting

    return Options(
        min_size_mb=get_setting(session, MIN_SIZE_KEY, DEFAULT_MIN_SIZE_MB),
        max_size_mb=get_setting(session, MAX_SIZE_KEY, 0) or 0,
        min_age_min=get_setting(session, MIN_AGE_KEY, 0) or 0,
        retention_days=get_setting(session, RETENTION_KEY, 0) or 0,
        early_days=get_setting(session, EARLY_DAYS_KEY, DEFAULT_EARLY_DAYS),
        must_contain=tuple(get_setting(session, MUST_CONTAIN_KEY, []) or ()),
        must_not_contain=tuple(get_setting(session, MUST_NOT_CONTAIN_KEY, []) or ()),
    )


@dataclass
class Evaluation:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # flags on an accepted release
    magazine: Magazine | None = None
    issue_key: str | None = None
    fields: dict = field(default_factory=dict)
    rank: list = field(default_factory=list)
    parsed: ParsedRelease | None = None


def format_rank(fmt: str | None, true_pdf: bool, profile: FormatProfile | None) -> list[int]:
    """Lower is better: profile format order, then TruePDF if preferred."""
    formats = profile.formats if profile else list(FORMATS)
    rank = formats.index(fmt) if fmt in formats else len(formats)
    prefer_true = profile.prefer_true_pdf if profile else True
    return [rank, 0 if (prefer_true and true_pdf) else 1]


def _rank(
    parsed: ParsedRelease, mag: Magazine, size, published: datetime | None, agreements: int = 0
) -> list:
    """Lower is better: format, then agreeing clues, then newest, then largest."""
    fmt = parsed.formats[0] if parsed.formats else None
    stamp = published.timestamp() if published else 0
    return [
        *format_rank(fmt, parsed.true_pdf, mag.format_profile),
        -agreements,
        -stamp,
        -(size or 0),
    ]


def _in_monitored_range(mag: Magazine, cover: date | None, published, today: date) -> str | None:
    """None if an issue that is not in the database yet should be wanted, else why not."""
    mode = mag.monitor_mode or MonitorMode.NEW_ONLY
    span = generation_range(mag, today)
    if cover is not None and span is not None:
        return None if cover >= span[0] else f"before the monitored range ({span[0]})"
    when = cover or (published.date() if published else None)
    if mode is MonitorMode.ALL:
        return None
    if mode is MonitorMode.FROM_YEAR and mag.monitor_from_year:
        if when and when.year >= mag.monitor_from_year:
            return None
        return f"before {mag.monitor_from_year}"
    if when and when >= today - timedelta(days=NEW_ONLY_DAYS):
        return None
    return "not new (monitoring new issues only)"


def thirteen_reference(session: Session, mag: Magazine) -> Reference | None:
    """Newest known numbered issue with a month (or the anchor), for month estimates."""
    if mag.frequency is not Frequency.THIRTEEN_PER_YEAR or mag.id is None:
        return None
    dated = session.scalars(
        select(Issue)
        .where(
            Issue.magazine_id == mag.id,
            Issue.issue_number.is_not(None),
            Issue.cover_date.is_not(None),
        )
        .order_by(Issue.issue_number.desc())
        .limit(1)
    ).first()
    newest = session.scalar(select(func.max(Issue.issue_number)).where(Issue.magazine_id == mag.id))
    if dated is not None and (mag.anchor_number is None or dated.issue_number >= mag.anchor_number):
        number, cover = dated.issue_number, dated.cover_date
    elif mag.anchor_number is not None and mag.anchor_date is not None:
        number, cover = mag.anchor_number, mag.anchor_date
    else:
        return None
    return Reference(number=number, cover=cover, newest=max(newest or 0, number))


def evaluate(
    session: Session,
    title: str,
    magazines: list[Magazine],
    today: date,
    *,
    size: int | None = None,
    protocol: str | None = "usenet",
    published: datetime | None = None,
    options: Options | None = None,
    guid: str | None = None,
    now: datetime | None = None,
) -> Evaluation:
    options = options or Options()
    reasons: list[str] = []
    if protocol and protocol != "usenet":
        reasons.append(f"protocol {protocol} (usenet only)")
    if size is not None and size < options.min_size_mb * 1024 * 1024:
        reasons.append(f"size {size / 1048576:.1f} MB is under {options.min_size_mb} MB")
    if size is not None and options.max_size_mb and size > options.max_size_mb * 1024 * 1024:
        reasons.append(f"size {size / 1048576:.1f} MB is over {options.max_size_mb:g} MB")
    undated = False
    if options.min_age_min or options.retention_days:
        if published is None:
            undated = True
        else:
            posted = published if published.tzinfo else published.replace(tzinfo=UTC)
            age = (now or datetime.now(UTC)) - posted
            if options.min_age_min and age < timedelta(minutes=options.min_age_min):
                minutes = max(0, int(age.total_seconds() // 60))
                reasons.append(
                    f"posted {minutes} min ago; the minimum age is {options.min_age_min} min"
                )
            if options.retention_days and age > timedelta(days=options.retention_days):
                reasons.append(
                    f"posted {age.days} days ago, beyond the {options.retention_days} day retention"
                )

    parsed = parse_release(title, TitleIndex.from_magazines(magazines))
    ev = Evaluation(accepted=False, reasons=reasons, parsed=parsed)
    if parsed.pack:
        reasons.append("multi-issue pack (not grabbed automatically)")
    reasons += [f"name: {r}" for r in parsed.reasons if r != "multi-issue pack"]

    results = [(m, match_title(parsed, m.title_spec())) for m in magazines]
    matches = [m for m, r in results if r.accepted]
    if not matches:
        near = [
            f"{m.title}: {r.reason}"
            for m, r in results
            if not r.reason.startswith(("title", "no title"))
        ]
        reasons.append("; ".join(near) if near else f"no tracked magazine is {parsed.title!r}")
        return ev
    if len(matches) > 1:
        reasons.append("matches " + ", ".join(m.title for m in matches))
        return ev
    mag = ev.magazine = matches[0]

    from periodicool.downloads import blocked

    if row := blocked(session, guid, title, mag.id):
        reasons.append(f"blocklisted: {row.reason}")

    lowered = title.lower()
    for term in mag.must_contain or []:
        if term.lower() not in lowered:
            reasons.append(f"missing required term {term!r}")
    for term in mag.must_not_contain or []:
        if term.lower() in lowered:
            reasons.append(f"contains rejected term {term!r}")
    for term in options.must_contain:
        if term.lower() not in lowered:
            reasons.append(f"missing required term {term!r} (all magazines)")
    for term in options.must_not_contain:
        if term.lower() in lowered:
            reasons.append(f"contains rejected term {term!r} (all magazines)")

    profile = mag.format_profile
    if parsed.formats and profile and not set(parsed.formats) & set(profile.formats):
        reasons.append(f"format {', '.join(parsed.formats)} not in profile {profile.name}")

    ident = identify(parsed, mag, thirteen_reference(session, mag))
    ev.fields = ident.fields
    if ident.key is None or ident.reasons:
        reasons += [r for r in ident.reasons if r not in parsed.reasons]  # parser's already listed
        if not reasons:
            reasons.append("no issue key")
        return ev
    ev.issue_key = ident.key
    ev.notes = list(ident.notes) + [f"agrees: {a}" for a in ident.agreements]
    if undated:
        ev.notes.append("no posting date, so minimum age and retention were not checked")
    ev.rank = _rank(parsed, mag, size, published, len(ident.agreements))

    if mag.monitored is False:
        reasons.append("magazine is not monitored")
    cover = ident.fields.get("cover_date")
    issue = session.scalars(
        select(Issue).where(Issue.magazine_id == mag.id, Issue.key == ident.key)
    ).one_or_none()
    if issue is not None:
        if issue.file_path:
            reasons.append(f"already have {ident.key}")
        elif not issue.monitored:
            reasons.append(f"{ident.key} is not monitored")
        elif issue.status in (IssueStatus.GRABBED, IssueStatus.DOWNLOADED, IssueStatus.IMPORTED):
            reasons.append(f"{ident.key} is already {issue.status.value}")
        number = ident.fields.get("issue_number")
        if ident.fields.get("estimated") and issue.issue_number not in (None, number):
            reasons.append(f"{ident.key} already holds issue {issue.issue_number}")
        cover = issue.cover_date or cover
    elif why := _in_monitored_range(mag, cover, published, today):
        reasons.append(why)
    # A magazine's own on-sale lead time, when set, replaces the global window.
    early = mag.on_sale_days + 7 if mag.on_sale_days is not None else options.early_days
    if cover and cover > today + timedelta(days=early):
        reasons.append(f"cover date {cover} is more than {early} days away")

    ev.accepted = not reasons
    return ev


def best_candidates(session: Session) -> list[Release]:
    """The best accepted release for each issue."""
    best: dict[tuple[int, str], Release] = {}
    for rel in session.scalars(select(Release).where(Release.accepted.is_(True))):
        key = (rel.magazine_id, rel.issue_key)
        if key not in best or rel.rank < best[key].rank:
            best[key] = rel
    return sorted(best.values(), key=lambda r: (r.magazine_id, r.issue_key))
