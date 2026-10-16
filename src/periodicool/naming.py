"""Library file names: sanitizing and the naming template."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path, PurePosixPath

from periodicool.models import Frequency, Magazine

DEFAULT_TEMPLATE = "{Title}/{Year}/{Title} - {Date}.{ext}"

_RESERVED = re.compile(r'[*?"<>|]')
_SLASHES = re.compile(r"[/\\]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EDGE_DASHES = re.compile(r"^(?:\s*-\s*)+|(?:\s*-\s*)+$")


class UnsafePathError(ValueError):
    """A rendered path would leave the library root."""


def sanitize(name: str) -> str:
    """Make one path segment safe for Linux and SMB shares. Keeps Unicode."""
    s = re.sub(r":\s+", " - ", name)
    s = s.replace(":", "")
    s = _RESERVED.sub("", s)
    s = _SLASHES.sub("-", s)
    s = _CONTROL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(". ")


def date_label(mag: Magazine, fields: dict, key: str | None = None) -> str:
    """The {Date} token: 2023-11, 2023-07-10, 2015-Winter, Vol 05 or No 261."""
    cover = fields.get("cover_date")
    if isinstance(cover, str):
        cover = date.fromisoformat(cover)
    year = fields.get("year") or (cover.year if cover else None)
    month = fields.get("month") or (cover.month if cover and not fields.get("season") else None)
    label = ""
    if year and fields.get("season"):
        label = f"{year}-{fields['season'].capitalize()}"
    elif year and month:
        label = f"{year}-{month:02d}"
        day = fields.get("day")
        if day and (
            mag.frequency in (Frequency.WEEKLY, Frequency.BIWEEKLY, Frequency.IRREGULAR)
            or fields.get("special")
        ):
            label += f"-{day:02d}"
        elif key and mag.frequency is Frequency.THIRTEEN_PER_YEAR:
            # A second issue in one month (key 2022-08-2) keeps its suffix.
            m = re.fullmatch(r"\d{4}-\d{2}-(\d+)", key)
            if m:
                label += f"-{m.group(1)}"
    elif fields.get("volume_number") is not None:
        label = f"Vol {fields['volume_number']:02d}"
    elif fields.get("issue_number") is not None:
        label = f"No {fields['issue_number']}"
    if fields.get("special"):
        label = f"{label} - Special" if label else "Special"
    return label


def render(
    template: str, mag: Magazine, fields: dict, ext: str, key: str | None = None
) -> PurePosixPath:
    """Relative library path for an issue file. Empty segments are dropped."""
    cover = fields.get("cover_date")
    if isinstance(cover, str):
        cover = date.fromisoformat(cover)
    year = fields.get("year") or (cover.year if cover else None)
    values = {
        "Title": sanitize(mag.title),
        "Year": str(year) if year else "",
        "Month": f"{fields['month']:02d}" if fields.get("month") else "",
        "Day": f"{fields['day']:02d}" if fields.get("day") else "",
        "Season": (fields.get("season") or "").capitalize(),
        "Issue": str(fields["issue_number"]) if fields.get("issue_number") is not None else "",
        "Volume": str(fields["volume_number"]) if fields.get("volume_number") is not None else "",
        "Date": date_label(mag, fields, key),
        "ext": ext.lower().lstrip("."),
    }
    parts = []
    for raw in template.split("/"):
        segment = raw.format(**values)
        stem, dot, suffix = segment.rpartition(".") if raw.endswith("{ext}") else (segment, "", "")
        stem = sanitize(_EDGE_DASHES.sub("", stem))
        if not stem:
            continue
        parts.append(f"{stem}{dot}{suffix}" if dot else stem)
    if not parts:
        raise UnsafePathError("template rendered an empty path")
    path = PurePosixPath(*parts)
    if path.is_absolute() or any(p in ("", ".", "..") for p in path.parts):
        raise UnsafePathError(f"unsafe path {path}")
    return path


def inside(root: Path, rel: PurePosixPath) -> Path:
    """Join rel onto root and make sure the result stays inside root."""
    full = (root / Path(*rel.parts)).resolve()
    if not full.is_relative_to(root.resolve()):
        raise UnsafePathError(f"{rel} leaves {root}")
    return full
