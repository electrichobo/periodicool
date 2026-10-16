"""Accept or reject a release, with every reason.

Interactive search shows these reasons for every candidate, so `evaluate`
keeps checking after the first rejection wherever later checks still make
sense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool.matching import match_title
from periodicool.models import FormatProfile, Issue, IssueStatus, Magazine, MonitorMode, Release
from periodicool.parser import FORMATS, ParsedRelease, parse_release
from periodicool.schedule import generation_range, identify

MIN_SIZE_KEY = "release.min_size_mb"
EARLY_DAYS_KEY = "search.early_days"
DEFAULT_MIN_SIZE_MB = 1
DEFAULT_EARLY_DAYS = 45
# Irregular magazines on "new only" accept releases published this recently.
NEW_ONLY_DAYS = 60


@dataclass(frozen=True)
class Options:
    min_size_mb: float = DEFAULT_MIN_SIZE_MB
    early_days: int = DEFAULT_EARLY_DAYS


def load_options(session: Session) -> Options:
    from periodicool.library import get_setting

    return Options(
        min_size_mb=get_setting(session, MIN_SIZE_KEY, DEFAULT_MIN_SIZE_MB),
        early_days=get_setting(session, EARLY_DAYS_KEY, DEFAULT_EARLY_DAYS),
    )


@dataclass
class Evaluation:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
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


def _rank(parsed: ParsedRelease, mag: Magazine, size, published: datetime | None) -> list:
    fmt = parsed.formats[0] if parsed.formats else None
    stamp = published.timestamp() if published else 0
    return [*format_rank(fmt, parsed.true_pdf, mag.format_profile), -stamp, -(size or 0)]


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
) -> Evaluation:
    options = options or Options()
    reasons: list[str] = []
    if protocol and protocol != "usenet":
        reasons.append(f"protocol {protocol} (usenet only)")
    if size is not None and size < options.min_size_mb * 1024 * 1024:
        reasons.append(f"size {size / 1048576:.1f} MB is under {options.min_size_mb} MB")

    parsed = parse_release(title)
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

    lowered = title.lower()
    for term in mag.must_contain or []:
        if term.lower() not in lowered:
            reasons.append(f"missing required term {term!r}")
    for term in mag.must_not_contain or []:
        if term.lower() in lowered:
            reasons.append(f"contains rejected term {term!r}")

    profile = mag.format_profile
    if parsed.formats and profile and not set(parsed.formats) & set(profile.formats):
        reasons.append(f"format {', '.join(parsed.formats)} not in profile {profile.name}")

    ident = identify(parsed, mag)
    ev.fields = ident.fields
    if ident.key is None or ident.reasons:
        reasons += [r for r in ident.reasons if r not in parsed.reasons]  # parser's already listed
        if not reasons:
            reasons.append("no issue key")
        return ev
    ev.issue_key = ident.key
    ev.rank = _rank(parsed, mag, size, published)

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
        cover = issue.cover_date or cover
    elif why := _in_monitored_range(mag, cover, published, today):
        reasons.append(why)
    if cover and cover > today + timedelta(days=options.early_days):
        reasons.append(f"cover date {cover} is more than {options.early_days} days away")

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
