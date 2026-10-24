"""Magazine actions behind the web UI: validate the add/edit form, preview expected
issues, mass edit, delete (database rows only), and automatic search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool import downloads, notify
from periodicool.library import TEMPLATE_KEY, get_setting
from periodicool.models import FormatProfile, Frequency, Issue, Magazine, MonitorMode, Numbering
from periodicool.naming import DEFAULT_TEMPLATE, UnsafePathError, render
from periodicool.schedule import expected_issues, generation_range, is_regular
from periodicool.search import wanted_issues

TEXT_FIELDS = ("title", "region", "search_term", "issn")
LIST_FIELDS = ("aliases", "languages", "must_contain", "must_not_contain", "tags")
BOOL_FIELDS = ("monitored", "filler_optional", "special_prefix")
INT_FIELDS = ("anchor_number", "publish_weekday", "grace_days", "monitor_from_year", "on_sale_days")
DATE_FIELDS = ("anchor_date", "schedule_start")


def split_list(raw: str) -> list[str]:
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


def form_values(magazine: Magazine | None = None) -> dict:
    """Current values as form strings, for showing the form."""
    if magazine is None:
        return {
            "title": "",
            "frequency": "monthly",
            "numbering": "date",
            "monitored": True,
            "filler_optional": True,
            "special_prefix": True,
            "monitor_mode": "new_only",
            "grace_days": "14",
            "languages": "english",
        }
    out = {}
    for name in TEXT_FIELDS:
        out[name] = getattr(magazine, name) or ""
    for name in LIST_FIELDS:
        out[name] = ", ".join(getattr(magazine, name) or [])
    for name in BOOL_FIELDS:
        out[name] = bool(getattr(magazine, name))
    for name in INT_FIELDS:
        value = getattr(magazine, name)
        out[name] = "" if value is None else str(value)
    for name in DATE_FIELDS:
        value = getattr(magazine, name)
        out[name] = value.isoformat() if value else ""
    out["frequency"] = magazine.frequency.value
    out["numbering"] = magazine.numbering.value
    out["monitor_mode"] = magazine.monitor_mode.value
    out["format_profile_id"] = str(magazine.format_profile_id or "")
    return out


@dataclass
class Parsed:
    values: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_form(session: Session, form: dict[str, str]) -> Parsed:
    """Turn submitted strings into Magazine values, with one message per bad field."""
    p = Parsed(raw=dict(form))
    v, err = p.values, p.errors
    title = form.get("title", "").strip()
    if not title:
        err["title"] = "Enter a title."
    v["title"] = title
    for name in ("region", "search_term"):
        v[name] = form.get(name, "").strip() or None
    for name in LIST_FIELDS:
        v[name] = split_list(form.get(name, ""))
    v["languages"] = [x.lower() for x in v["languages"]] or ["english"]
    for name in BOOL_FIELDS:
        v[name] = form.get(name) in ("on", "true", "1")
    for name, enum in (("frequency", Frequency), ("numbering", Numbering)):
        try:
            v[name] = enum(form.get(name, ""))
        except ValueError:
            err[name] = "Pick one from the list."
    try:
        v["monitor_mode"] = MonitorMode(form.get("monitor_mode", "new_only"))
    except ValueError:
        err["monitor_mode"] = "Pick one from the list."
    for name in INT_FIELDS:
        raw = form.get(name, "").strip()
        if not raw:
            v[name] = None
            continue
        try:
            v[name] = int(raw)
        except ValueError:
            err[name] = "Must be a whole number."
    if v.get("grace_days") is None and "grace_days" not in err:
        v["grace_days"] = 14
    raw_issn = form.get("issn", "").strip()
    v["issn"] = None
    if raw_issn:
        from periodicool.issn import normalize

        v["issn"] = normalize(raw_issn)
        if v["issn"] is None:
            err["issn"] = "Not a valid ISSN (8 characters, like 0749-3851; the last may be X)."
    if v.get("on_sale_days") is not None and not 0 <= v["on_sale_days"] <= 120:
        err["on_sale_days"] = "0 to 120 days."
    if v.get("publish_weekday") is not None and not 0 <= v["publish_weekday"] <= 6:
        err["publish_weekday"] = "0 (Monday) to 6 (Sunday)."
    for name in DATE_FIELDS:
        raw = form.get(name, "").strip()
        try:
            v[name] = date.fromisoformat(raw) if raw else None
        except ValueError:
            err[name] = "Use YYYY-MM-DD."
    if v.get("monitor_mode") is MonitorMode.FROM_YEAR and not v.get("monitor_from_year"):
        err["monitor_from_year"] = "Enter the first year to monitor."
    if (
        v.get("numbering") is Numbering.ISSUE
        and v.get("frequency") not in (None, Frequency.IRREGULAR)
        and (v.get("anchor_number") is None or v.get("anchor_date") is None)
    ):
        err.setdefault("anchor_number", "Numbered magazines need a known issue number and date.")
    raw_profile = form.get("format_profile_id", "").strip()
    v["format_profile_id"] = None
    if raw_profile:
        profile = session.get(FormatProfile, int(raw_profile)) if raw_profile.isdigit() else None
        if profile is None:
            err["format_profile_id"] = "Pick one from the list."
        else:
            v["format_profile_id"] = profile.id
    return p


def apply_values(mag: Magazine, values: dict) -> Magazine:
    for name, value in values.items():
        setattr(mag, name, value)
    return mag


@dataclass
class Preview:
    folder: str | None
    example: str | None
    issues: list[tuple[str, str]]  # (key, cover date)
    note: str | None = None


def preview(session: Session, values: dict, today: date, limit: int = 8) -> Preview:
    """Expected issues and the folder name for unsaved form values. Writes nothing."""
    mag = Magazine(**{k: v for k, v in values.items() if k != "format_profile_id"})
    template = get_setting(session, TEMPLATE_KEY, DEFAULT_TEMPLATE)
    folder = example = note = None
    issues: list[tuple[str, str]] = []
    if not mag.title:
        return Preview(None, None, [], "Enter a title to see the folder name.")
    try:
        if is_regular(mag):
            span = generation_range(mag, today)
            start = span[0] if span else today
            end = span[1] if span else today
            # Show a little history even for "new only", so the cadence is visible.
            exp = expected_issues(mag, min(start, end - timedelta(days=400)), end)
            issues = [(e.key, e.cover_date.isoformat()) for e in exp[-limit:]]
            fields = vars(exp[-1]) if exp else {"year": today.year, "month": today.month}
            key = exp[-1].key if exp else None
        else:
            note = "Irregular: issues come from releases, not a schedule."
            fields, key = {"year": today.year, "volume_number": 1}, None
        path = render(template, mag, fields, "pdf", key=key)
        example = path.as_posix()
        folder = path.parts[0] if len(path.parts) > 1 else None
    except (UnsafePathError, ValueError, TypeError, AttributeError) as exc:
        note = f"Cannot preview yet: {exc}"
    return Preview(folder, example, issues, note)


def set_monitored(session: Session, mag: Magazine, monitored: bool) -> None:
    mag.monitored = monitored


def set_year_monitored(session: Session, mag: Magazine, year: int, monitored: bool) -> int:
    count = 0
    for issue in mag.issues:
        y = issue.cover_date.year if issue.cover_date else issue.year
        if y == year:
            issue.monitored = monitored
            count += 1
    return count


def unlink_issue(issue: Issue) -> str | None:
    """Forget the issue's file in the database. The file itself is never touched."""
    from periodicool.models import IssueStatus

    old = issue.file_path
    issue.file_path = None
    issue.file_size = issue.file_format = issue.imported_at = None
    issue.status = IssueStatus.WANTED
    return old


@dataclass
class MassEdit:
    monitored: bool | None = None
    monitor_mode: MonitorMode | None = None
    format_profile_id: int | None = None
    add_tags: list[str] = field(default_factory=list)
    remove_tags: list[str] = field(default_factory=list)


def mass_edit(session: Session, ids: list[int], edit: MassEdit) -> int:
    count = 0
    remove = {t.lower() for t in edit.remove_tags}
    for mag in session.scalars(select(Magazine).where(Magazine.id.in_(ids))):
        if edit.monitored is not None:
            mag.monitored = edit.monitored
        if edit.monitor_mode is not None:
            mag.monitor_mode = edit.monitor_mode
        if edit.format_profile_id is not None:
            mag.format_profile_id = edit.format_profile_id
        tags = [t for t in (mag.tags or []) if t.lower() not in remove]
        for tag in edit.add_tags:
            if tag.lower() not in {t.lower() for t in tags}:
                tags.append(tag)
        mag.tags = tags
        count += 1
    return count


def delete_magazines(session: Session, ids: list[int]) -> list[str]:
    """Remove magazines and their issue rows from the database. Files stay on disk."""
    titles = []
    for mag in session.scalars(select(Magazine).where(Magazine.id.in_(ids))):
        titles.append(mag.title)
        notify.magazine_event(session, "MagazineDelete", mag)
        session.delete(mag)
    session.flush()
    return titles


@dataclass
class AutoSearch:
    found: int = 0
    grabbed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def automatic_search(
    session: Session,
    mag: Magazine,
    today: date,
    prowlarr,
    client,
    issue_key: str | None = None,
) -> AutoSearch:
    """Search the magazine, then grab the best release for each wanted issue
    (or only issue_key). The user asked for it, so this does not wait for grab.auto."""
    from periodicool.search import search_magazine

    out = AutoSearch()
    result = search_magazine(session, prowlarr, mag, today)
    out.found = sum(1 for _, ev in result.candidates if ev.accepted)
    if client is None:
        out.problems.append("no download client is set up, so nothing was sent")
        return out
    keys = [issue_key] if issue_key else [i.key for i in wanted_issues(session, mag, today)]
    candidates = downloads._candidates(session)
    for key in keys:
        releases = candidates.get((mag.id, key), [])
        if not releases:
            continue
        res = downloads._grab_first(session, releases, prowlarr, client, today)
        if res and res.ok:
            out.grabbed.append(key)
        elif res:
            out.problems.extend(res.reasons[:1])
    return out
