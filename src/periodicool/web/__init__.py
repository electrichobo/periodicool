"""Web UI: server-rendered pages with htmx for partial updates."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from periodicool import __version__, overview

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


def when(moment, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if moment is None:
        return ""
    from datetime import UTC
    from zoneinfo import ZoneInfo

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(DISPLAY_TZ["name"])
    except KeyError, ValueError:
        zone = UTC
    return moment.astimezone(zone).strftime(fmt)


templates.env.filters["when"] = when
# Sum of the numeric parts of a chart row (label, have, wanted, missing).
templates.env.filters["sum_parts"] = lambda row: sum(row[1:])

router = APIRouter()


def render(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("csrf_token", getattr(request.state, "csrf_token", ""))
    context.setdefault("user", getattr(request.state, "user", None))
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
