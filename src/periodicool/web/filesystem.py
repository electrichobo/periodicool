"""Folder browser for path fields (remote path mappings, backup and log folders).

Lists folders only, never files, on the machine PeriodiCool runs on (inside the
container, that is the container's view). Like every page, it needs a login.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from periodicool.web.magazines import _render

router = APIRouter(include_in_schema=False)

MAX_ENTRIES = 1000
_TARGET = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _start(raw: str) -> Path:
    """The folder to show: the given one, else its nearest existing parent, else the root."""
    path = Path(raw.strip() or "/")
    if not path.is_absolute():
        path = Path("/") / path
    while not path.is_dir() and path.parent != path:
        path = path.parent
    return path if path.is_dir() else Path(path.anchor or "/")


def list_folders(path: Path) -> tuple[list[Path], str | None]:
    try:
        folders = sorted(
            (p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
    except OSError as exc:
        return [], f"Cannot read this folder: {exc.strerror or exc}"
    return folders[:MAX_ENTRIES], None


@router.get("/filesystem", response_class=HTMLResponse)
def browse(request: Request, path: str = "", target: str = "") -> HTMLResponse:
    if not _TARGET.fullmatch(target):
        target = ""
    current = _start(path)
    folders, error = list_folders(current)
    parent = current.parent if current.parent != current else None
    return _render(
        request,
        "_folder_picker.html",
        current=current.as_posix(),
        parent=parent.as_posix() if parent else None,
        folders=[(f.name, f.as_posix()) for f in folders],
        error=error,
        target=target,
        truncated=len(folders) >= MAX_ENTRIES,
    )
