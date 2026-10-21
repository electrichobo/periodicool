"""The settings shown in the UI: one list of fields per tab, with types, limits and help.

Secrets (API keys) never appear here; they come from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

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
        ],
    ),
    "indexers": (
        "Indexers",
        [
            Field("prowlarr.url", "Prowlarr URL", "url", None, "For example http://prowlarr:9696"),
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
                "Wait between Prowlarr requests",
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
            Field("sabnzbd.url", "SABnzbd URL", "url", None, "For example http://sabnzbd:8080"),
            Field(
                "sabnzbd.category",
                "Category",
                "text",
                "",
                "PeriodiCool adds jobs only in this category. Create it in SABnzbd first.",
            ),
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
                "backup.interval_days",
                "Back up every",
                "int",
                7,
                "",
                min=1,
                max=90,
                unit="days",
            ),
            Field("backup.keep", "Backups to keep", "int", 7, "", min=1, max=100),
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
        if not raw:
            return None, None
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return UNSET, "Use a full address starting with http:// or https://"
        if parts.username or parts.password or "apikey" in parts.query.lower():
            return UNSET, "Leave keys and passwords out; they come from the environment."
        return raw.rstrip("/"), None
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
        if field.key == "backup.interval_days":
            job = session.get(Job, "backup")
            if job is not None:
                job.interval_s = int(value) * 86400
    return {}


def path_map(session: Session) -> list[tuple[str, str]]:
    from periodicool.downloads import PATH_MAP_KEY

    return [tuple(pair) for pair in get_setting(session, PATH_MAP_KEY, []) or []]


def save_path_map(session: Session, remotes: list[str], locals_: list[str]) -> str | None:
    from periodicool.downloads import PATH_MAP_KEY

    pairs = []
    for remote, local in zip(remotes, locals_, strict=False):
        remote, local = remote.strip(), local.strip()
        if not remote and not local:
            continue
        if not remote or not local:
            return "Each mapping needs both the client's path and PeriodiCool's path."
        pairs.append([remote.rstrip("/") or "/", local.rstrip("/\\") or "/"])
    set_setting(session, PATH_MAP_KEY, pairs)
    return None


def job_names(session: Session) -> list[str]:
    return list(session.scalars(select(Job.name)))
