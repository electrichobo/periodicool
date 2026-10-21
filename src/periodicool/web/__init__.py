"""Web UI: server-rendered pages with htmx for partial updates."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from periodicool import __version__

PACKAGE = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE / "static"
TEMPLATES_DIR = PACKAGE / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["version"] = __version__
templates.env.globals["static"] = lambda path: f"/static/{path}?v={__version__}"


def filesize(num) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


templates.env.filters["filesize"] = filesize
# Sum of the numeric parts of a chart row (label, have, wanted, missing).
templates.env.filters["sum_parts"] = lambda row: sum(row[1:])

router = APIRouter()


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


@router.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    # The magazine overview arrives with the screens (8c); the kit is the preview until then.
    return RedirectResponse("/ui-kit", status_code=307)


@router.get("/ui-kit", response_class=HTMLResponse, include_in_schema=False)
def ui_kit(request: Request) -> HTMLResponse:
    return render(request, "ui_kit.html", active="/ui-kit")
