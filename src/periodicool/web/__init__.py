"""Web UI: server-rendered pages with htmx for partial updates."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from periodicool import __version__, dates, overview

PACKAGE = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE / "static"
TEMPLATES_DIR = PACKAGE / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["version"] = __version__


def static_url(path: str) -> str:
    # Stamp with the file's mtime so browsers refetch after any change.
    try:
        stamp = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        stamp = __version__
    return f"/static/{path}?v={stamp}"


templates.env.globals["static"] = static_url
templates.env.globals["event_labels"] = overview.EVENT_LABELS
templates.env.globals["event_leds"] = overview.EVENT_LEDS


def filesize(num) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


templates.env.filters["filesize"] = filesize


def relative_to(path, root) -> str:
    """A path shown relative to a root folder, or as given when it is outside it."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError, OSError:
        return str(path)


templates.env.filters["relative_to"] = relative_to

# Times are stored in UTC and shown in the TZ setting; create_app sets this.
DISPLAY_TZ = {"name": "UTC"}
# Light, dark or auto (follow the system); loaded at start and when Settings, UI is saved.
DISPLAY_THEME = {"name": "auto"}


def local(moment):
    from datetime import UTC
    from zoneinfo import ZoneInfo

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(DISPLAY_TZ["name"])
    except KeyError, ValueError:
        zone = UTC
    return moment.astimezone(zone)


def when(moment, fmt: str | None = None) -> str:
    """A stored time in the display time zone: the short date and the clock as set in
    Settings, UI, or an explicit strftime format."""
    if moment is None:
        return ""
    moment = local(moment)
    if fmt:
        return moment.strftime(fmt)
    return f"{dates.short_date(moment)} {dates.clock(moment)}"


templates.env.filters["when"] = when


def urlhost(url: str) -> str:
    """Only the host of an address, for lists (paths can carry tokens)."""
    import urllib.parse

    return urllib.parse.urlsplit(url or "").hostname or ""


templates.env.filters["urlhost"] = urlhost
templates.env.filters["shortdate"] = dates.short_date
templates.env.filters["longdate"] = dates.long_date
templates.env.filters["weekheader"] = dates.week_header
templates.env.filters["clock"] = lambda moment: dates.clock(local(moment)) if moment else ""
# Sum of the numeric parts of a chart row (label, have, wanted, missing).
templates.env.filters["sum_parts"] = lambda row: sum(row[1:])

router = APIRouter()


SYS_STATE_SECONDS = 60
SYS_STATE_TEXT = {"ok": "Sys nominal", "warn": "Sys warning", "bad": "Sys fault"}


def system_state(request: Request) -> tuple[str, str] | None:
    """The header light: the worst local health check, refreshed once a minute."""
    import time

    state = request.app.state
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return None
    now = time.monotonic()
    cached = getattr(state, "sys_state", None)
    if cached is not None and now - cached[0] < SYS_STATE_SECONDS:
        return cached[1]
    from periodicool import health

    try:
        with sessions() as session:
            kinds = {c.kind for c in health.local_checks(session, state.settings)}
    except Exception:  # noqa: BLE001  the header must never break a page
        return None
    kind = "bad" if "bad" in kinds else "warn" if "warn" in kinds else "ok"
    result = (kind, SYS_STATE_TEXT[kind])
    state.sys_state = (now, result)
    return result


def render(request: Request, name: str, /, **context) -> HTMLResponse:
    context.setdefault("csrf_token", getattr(request.state, "csrf_token", ""))
    context.setdefault("user", getattr(request.state, "user", None))
    if context.get("nav", True):
        context.setdefault("sys_state", system_state(request))
    context.setdefault("ui_theme", DISPLAY_THEME["name"])
    return templates.TemplateResponse(request, name, context)


@router.get("/ui-kit", response_class=HTMLResponse, include_in_schema=False)
def ui_kit(request: Request) -> HTMLResponse:
    return render(request, "ui_kit.html", active="/ui-kit")


@router.get("/ui-kit/modal", response_class=HTMLResponse, include_in_schema=False)
def ui_kit_modal(request: Request) -> HTMLResponse:
    return render(request, "_kit_modal.html")


@router.post("/ui-kit/toast", include_in_schema=False)
def ui_kit_toast() -> HTMLResponse:
    trigger = {"toast": {"kind": "ok", "text": "Settings saved."}}
    return HTMLResponse("", headers={"HX-Trigger": json.dumps(trigger)})


@router.get("/readout", include_in_schema=False)
def readout() -> dict:
    """What is running now and what just finished, for the readout under the nav."""
    import time

    from periodicool import activity_feed

    now = time.monotonic()
    running, recent = activity_feed.snapshot()
    return {
        "running": [
            {"id": t.id, "title": t.title, "line": t.line, "seconds": int(now - t.started)}
            for t in running
        ],
        "recent": [{"id": t.id, "title": t.title, "line": t.line, "ok": t.ok} for t in recent],
    }
