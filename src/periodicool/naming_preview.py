"""Check a naming template and show it applied to sample issues of each kind."""

from __future__ import annotations

from datetime import date

from periodicool.models import Frequency, Magazine, Numbering
from periodicool.naming import UnsafePathError, render

TOKENS = (
    ("{Title}", "Magazine title, made safe for file names"),
    ("{Year}", "Year of the issue"),
    ("{Month}", "Month, two digits"),
    ("{Day}", "Day, two digits (weeklies)"),
    ("{Season}", "Spring, Summer, Autumn or Winter"),
    ("{Issue}", "Issue number"),
    ("{Volume}", "Volume number"),
    ("{Date}", "The usual label: 2026-10, 2026-10-16, 2026-Autumn, Vol 05 or No 475"),
    ("{ext}", "File extension: pdf, epub, cbz or cbr"),
)


def _mag(title, frequency, numbering) -> Magazine:
    return Magazine(title=title, frequency=frequency, numbering=numbering)


SAMPLES = (
    (
        "Monthly",
        _mag("American Cinematographer", Frequency.MONTHLY, Numbering.DATE),
        {"year": 2026, "month": 10, "cover_date": date(2026, 10, 1)},
        "2026-10",
    ),
    (
        "Weekly",
        _mag("People", Frequency.WEEKLY, Numbering.DATE),
        {"year": 2026, "month": 10, "day": 16, "cover_date": date(2026, 10, 16)},
        "2026-10-16",
    ),
    (
        "Seasonal",
        _mag("2600: The Hacker Quarterly", Frequency.SEASONAL, Numbering.SEASON),
        {"year": 2026, "season": "autumn", "cover_date": date(2026, 9, 1)},
        "2026-autumn",
    ),
    (
        "Numbered",
        _mag("Fortean Times", Frequency.THIRTEEN_PER_YEAR, Numbering.ISSUE),
        {"year": 2026, "month": 10, "issue_number": 475, "cover_date": date(2026, 10, 1)},
        "2026-10",
    ),
    (
        "Volume",
        _mag("Make:", Frequency.IRREGULAR, Numbering.VOLUME),
        {"year": 2026, "volume_number": 95},
        "v95",
    ),
    (
        "Special",
        _mag("People", Frequency.WEEKLY, Numbering.DATE),
        {"year": 2026, "month": 7, "day": 7, "special": True, "cover_date": date(2026, 7, 7)},
        "special-2026-07-07",
    ),
)


def examples(template: str) -> list[tuple[str, str]]:
    out = []
    for label, mag, fields, key in SAMPLES:
        try:
            out.append((label, render(template, mag, fields, "pdf", key=key).as_posix()))
        except (UnsafePathError, KeyError, ValueError, IndexError) as exc:
            out.append((label, f"error: {exc}"))
    return out


def template_problem(template: str) -> str | None:
    if not template:
        return "Enter a template."
    mag = _mag("Sample", Frequency.MONTHLY, Numbering.DATE)
    try:
        a = render(template, mag, {"year": 2026, "month": 10}, "pdf", key="2026-10")
        b = render(template, mag, {"year": 2026, "month": 11}, "pdf", key="2026-11")
        for _, sample_mag, fields, key in SAMPLES:
            render(template, sample_mag, fields, "pdf", key=key)
    except KeyError as exc:
        return f"Unknown token {{{exc.args[0]}}}. See the list of tokens."
    except (UnsafePathError, ValueError, IndexError) as exc:
        return f"This template does not work: {exc}"
    if not template.endswith(".{ext}"):
        return "End the template with .{ext} so files keep their extension."
    if not any(t in template for t in ("{Date}", "{Issue}", "{Volume}")):
        return "Include {Date}, {Issue} or {Volume} so every issue gets its own name."
    if a == b:
        return "Two issues would get the same name. Add {Date} or {Month}."
    return None
