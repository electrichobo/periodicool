"""The settings shown in the UI: one list of fields per tab, with types, limits and help.

Indexer and download client entries (with their write-only keys) live in their own
tables and have their own forms (web/sources.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool.dates import LONG_CHOICES, SHORT_CHOICES, TIME_CHOICES, WEEK_HEADER_CHOICES
from periodicool.library import get_setting, set_setting
from periodicool.models import Job
from periodicool.naming import DEFAULT_TEMPLATE

UNSET = object()


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # text, url, int, float, bool, choice, list, intlist, template
    default: object = None
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    scale: int = 1  # stored value = shown value * scale (minutes or hours shown, seconds stored)
    unit: str = ""
    optional: bool = False  # empty input stores None

    @property
    def name(self) -> str:
        return self.key.replace(".", "__")


SECTIONS: dict[str, tuple[str, list[Field]]] = {
    "media": (
        "Media management",
        [
            Field(
                "naming.template",
                "File naming",
                "template",
                DEFAULT_TEMPLATE,
                "Where each issue goes in the library. Folders are separated by /.",
            ),
            Field(
                "downloads.remove_leftovers",
                "Remove leftover download folders",
                "bool",
                True,
                "After a verified import, remove the job's own folder in the download folder.",
            ),
            Field(
                "import.auto_apply",
                "Import from the drop folder automatically",
                "bool",
                True,
                "Confident matches in the import folder move into the library on their own.",
            ),
            Field(
                "pdf.read_text",
                "Read PDF text when a name is unclear",
                "bool",
                True,
                "Looks at the first pages for an ISSN, cover date and issue number. "
                "Local only; files that cannot be read are skipped.",
            ),
            Field(
                "pdf.pages",
                "PDF pages to read",
                "int",
                2,
                "More pages find more clues but take longer.",
                min=1,
                max=5,
            ),
        ],
    ),
    "indexers": (
        "Indexers",
        [
            Field(
                "search.categories",
                "Categories",
                "intlist",
                [7010],
                "Newznab categories to search, comma separated. 7010 is magazines.",
            ),
            Field(
                "jobs.rss.interval_s",
                "RSS sync every",
                "int",
                15 * 60,
                "How often new releases are read from the indexers' RSS feeds.",
                min=5,
                max=24 * 60,
                scale=60,
                unit="minutes",
            ),
            Field(
                "jobs.wanted.interval_s",
                "Search for wanted issues every",
                "int",
                6 * 3600,
                "Scheduled searches, five magazines at a time, with backoff per magazine.",
                min=1,
                max=7 * 24,
                scale=3600,
                unit="hours",
            ),
            Field(
                "search.cache_hours",
                "Keep search results for",
                "int",
                6,
                "Searches repeated within this time use the saved results, not API hits.",
                min=0,
                max=168,
                unit="hours",
            ),
            Field(
                "search.min_interval_s",
                "Wait between requests to an indexer",
                "float",
                2.0,
                "",
                min=0,
                max=60,
                unit="seconds",
            ),
            Field(
                "search.daily_cap",
                "Daily search limit",
                "int",
                None,
                "Optional. Leave empty to rely on each indexer's own limits in Prowlarr.",
                min=1,
                max=10000,
                optional=True,
            ),
            Field(
                "search.early_days",
                "Accept issues dated up to",
                "int",
                45,
                "Magazines appear before their cover date. Later cover dates are rejected.",
                min=0,
                max=180,
                unit="days ahead",
            ),
            Field(
                "release.min_size_mb",
                "Minimum release size",
                "float",
                1,
                "",
                min=0,
                max=10000,
                unit="MB",
            ),
            Field(
                "release.max_size_mb",
                "Maximum release size",
                "float",
                0,
                "Larger releases are rejected. 0 means no limit.",
                min=0,
                max=100000,
                unit="MB",
                optional=True,
            ),
            Field(
                "release.min_age_min",
                "Minimum age",
                "int",
                0,
                "Wait this long after a release is posted before grabbing it, so it can "
                "finish propagating. 0 means no wait.",
                min=0,
                max=1440,
                unit="minutes",
                optional=True,
            ),
            Field(
                "release.retention_days",
                "Retention",
                "int",
                0,
                "Your usenet provider's retention. Older releases are rejected. 0 means no limit.",
                min=0,
                max=10000,
                unit="days",
                optional=True,
            ),
            Field(
                "release.must_contain",
                "Every release must contain",
                "list",
                [],
                "Comma separated terms, for all magazines (each magazine can add its own).",
            ),
            Field(
                "release.must_not_contain",
                "Reject releases containing",
                "list",
                [],
                "Comma separated terms, for all magazines.",
            ),
        ],
    ),
    "downloads": (
        "Download clients",
        [
            Field(
                "grab.auto",
                "Grab automatically",
                "bool",
                False,
                "Send the best release for each wanted issue without asking. Off by default.",
            ),
            Field(
                "grab.max_per_run",
                "Most grabs per run",
                "int",
                10,
                "A safety limit for automatic grabs.",
                min=1,
                max=100,
            ),
            Field(
                "jobs.downloads.interval_s",
                "Check downloads every",
                "int",
                60,
                "",
                min=15,
                max=3600,
                unit="seconds",
            ),
        ],
    ),
    "metadata": (
        "Metadata",
        [
            Field(
                "lookups.loc",
                "Library of Congress",
                "bool",
                False,
                "Catalog records: title, other titles, publisher, frequency, first issue. "
                "Sends the ISSN (or the title when there is none).",
            ),
            Field(
                "lookups.wikidata",
                "Wikidata",
                "bool",
                False,
                "Title, other names, ISSN, start year, language, country, website. "
                "Sends the ISSN (or the title when there is none).",
            ),
            Field(
                "lookups.archive",
                "Internet Archive",
                "bool",
                False,
                "Lists of past issues, for magazines you monitor back to a year. "
                "Sends the title. Only titles and dates are read, never files.",
            ),
        ],
    ),
    "general": (
        "General",
        [
            Field(
                "auth.mode",
                "Login",
                "choice",
                "required",
                "Local skips login for private and loopback addresses only.",
                choices=(
                    ("required", "Required"),
                    ("local", "Not needed on the local network"),
                    ("off", "Off (only on a network you trust)"),
                ),
            ),
            Field(
                "auth.trust_proxy",
                "Trust the reverse proxy",
                "bool",
                False,
                "Read the visitor's address from X-Forwarded-For. Only behind your own proxy.",
            ),
            Field(
                "log.level",
                "Log level",
                "choice",
                "info",
                "Takes effect after a restart.",
                choices=(("info", "Info"), ("warning", "Warnings and errors"), ("error", "Errors")),
            ),
            Field(
                "server.port",
                "Port",
                "int",
                8383,
                "Takes effect after a restart. In Docker, the port mapping (and the container's "
                "health check) must use the same number.",
                min=1,
                max=65535,
                optional=True,
            ),
            Field(
                "server.url_base",
                "URL base",
                "urlbase",
                "",
                "For a reverse proxy that serves PeriodiCool under a path, for example "
                "/periodicool. Leave empty otherwise. Takes effect after a restart.",
            ),
            Field(
                "server.allowed_hosts",
                "Allowed hosts",
                "list",
                [],
                "Host names PeriodiCool answers to, comma separated (localhost always works). "
                "Empty allows any. Takes effect after a restart.",
            ),
        ],
    ),
    # Not settings tabs: these are edited on System, Backups and System, Log files.
    "backups": (
        "Backups",
        [
            Field(
                "backup.interval_days", "Back up every", "int", 7, "", min=1, max=90, unit="days"
            ),
            Field("backup.keep", "Backups to keep", "int", 7, "", min=1, max=100),
            Field(
                "backup.folder",
                "Backup folder",
                "folder",
                None,
                "Leave empty for /config/backups.",
                optional=True,
            ),
        ],
    ),
    "logs": (
        "Log files",
        [
            Field(
                "log.folder",
                "Log folder",
                "folder",
                None,
                "Leave empty for /config/logs.",
                optional=True,
            ),
        ],
    ),
    "ui": (
        "UI",
        [
            Field(
                "ui.first_weekday",
                "First day of the week",
                "choice",
                "0",
                "For the calendar.",
                choices=(("0", "Monday"), ("6", "Sunday")),
            ),
            Field(
                "ui.week_header",
                "Week column header",
                "choice",
                "ddd m/d",
                "The day headings in the calendar's week view.",
                choices=WEEK_HEADER_CHOICES,
            ),
            Field("ui.short_date", "Short date format", "choice", "iso", "", choices=SHORT_CHOICES),
            Field(
                "ui.long_date",
                "Long date format",
                "choice",
                "day-mon-d-y",
                "",
                choices=LONG_CHOICES,
            ),
            Field("ui.time_format", "Time format", "choice", "24", "", choices=TIME_CHOICES),
            Field(
                "ui.relative_dates",
                "Show relative dates",
                "bool",
                False,
                "Today, Yesterday and Tomorrow instead of the date.",
            ),
            Field(
                "ui.theme",
                "Theme",
                "choice",
                "auto",
                "Auto follows your device's light or dark setting.",
                choices=(("auto", "Auto"), ("light", "Light"), ("dark", "Dark")),
            ),
        ],
    ),
}


def shown_value(session: Session, field: Field):
    value = get_setting(session, field.key, field.default)
    if field.kind == "bool":
        return bool(value)
    if value is None:
        return ""
    if field.kind in ("list", "intlist"):
        return ", ".join(str(v) for v in value or [])
    if field.scale != 1 and isinstance(value, int | float):
        shown = value / field.scale
        return int(shown) if shown == int(shown) else shown
    return value


def values(session: Session, section: str) -> dict:
    return {f.name: shown_value(session, f) for f in SECTIONS[section][1]}


def _number(field: Field, raw: str):
    try:
        value = int(raw) if field.kind == "int" else float(raw)
    except ValueError:
        return UNSET, "Must be a whole number." if field.kind == "int" else "Must be a number."
    if field.min is not None and value < field.min:
        return UNSET, f"At least {field.min:g}."
    if field.max is not None and value > field.max:
        return UNSET, f"At most {field.max:g}."
    return value * field.scale, None


def parse_url(raw: str) -> tuple[str | None, str | None]:
    """(address without a trailing slash, problem). Keys never belong in the address."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, "Use a full address starting with http:// or https://"
    if parts.username or parts.password or "apikey" in parts.query.lower():
        return None, "Leave keys and passwords out of the address; they have their own field."
    return raw.rstrip("/"), None


def parse(field: Field, form: dict[str, str]):
    """(value to store, error). Bools are absent from the form when off."""
    raw = form.get(field.name, "").strip()
    if field.kind == "bool":
        return raw in ("on", "true", "1"), None
    if field.kind in ("int", "float"):
        if not raw:
            return (None, None) if field.optional else (UNSET, "Required.")
        return _number(field, raw)
    if field.kind == "choice":
        allowed = {c for c, _ in field.choices}
        return (raw, None) if raw in allowed else (UNSET, "Pick one from the list.")
    if field.kind == "list":
        return [p.strip() for p in raw.split(",") if p.strip()], None
    if field.kind == "intlist":
        try:
            items = [int(p) for p in raw.replace(" ", "").split(",") if p]
        except ValueError:
            return UNSET, "Numbers separated by commas."
        return (items, None) if items else (UNSET, "At least one category.")
    if field.kind == "url":
        value, problem = parse_url(raw)
        return (UNSET, problem) if problem else (value, None)
    if field.kind == "folder":
        if not raw:
            return None, None
        path = Path(raw)
        if not path.is_absolute():
            return UNSET, "Use a full path, such as /config/backups."
        if path.exists() and not path.is_dir():
            return UNSET, "That is a file, not a folder."
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return UNSET, f"Cannot create or use this folder: {exc.strerror or exc}"
        return path.as_posix(), None
    if field.kind == "urlbase":
        from periodicool.web.urlbase import clean_base

        if raw and not re.fullmatch(r"/?[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*/?", raw):
            return UNSET, "Use a path such as /periodicool (letters, numbers, dots and dashes)."
        return clean_base(raw), None
    if field.kind == "template":
        from periodicool.naming_preview import template_problem

        if problem := template_problem(raw):
            return UNSET, problem
        return raw, None
    return raw, None


def save(session: Session, section: str, form: dict[str, str]) -> dict[str, str]:
    """Store every valid field. Returns errors by field name; nothing is stored if any."""
    parsed, errors = [], {}
    for field in SECTIONS[section][1]:
        value, error = parse(field, form)
        if error:
            errors[field.name] = error
        else:
            parsed.append((field, value))
    if errors:
        return errors
    for field, value in parsed:
        set_setting(session, field.key, value)
        if field.key.startswith("jobs.") and field.key.endswith(".interval_s"):
            job = session.get(Job, field.key.split(".")[1])
            if job is not None:
                job.interval_s = int(value)
        if section == "ui":
            apply_display(session)
        if field.key == "backup.interval_days":
            job = session.get(Job, "backup")
            if job is not None:
                job.interval_s = int(value) * 86400
    return {}


def apply_folders(session: Session) -> None:
    """Load the backup and log folders chosen on the System pages."""
    from periodicool import backup, logs

    backup.FOLDER["path"] = get_setting(session, backup.FOLDER_KEY) or None
    logs.FOLDER["path"] = get_setting(session, logs.FOLDER_KEY) or None


def apply_display(session: Session) -> None:
    """Load the date formats and theme the pages use."""
    from periodicool import dates, web

    dates.load(session)
    web.DISPLAY_THEME["name"] = get_setting(session, "ui.theme", "auto")


def job_names(session: Session) -> list[str]:
    return list(session.scalars(select(Job.name)))
