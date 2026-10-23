"""How dates and times are shown, per Settings, UI (Sonarr's choices)."""

from __future__ import annotations

from datetime import date, datetime

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

SHORT_CHOICES = (
    ("iso", "2026-10-22"),
    ("mon-d-y", "Oct 22 2026"),
    ("d-mon-y", "22 Oct 2026"),
    ("m/d/y", "10/22/2026"),
    ("d/m/y", "22/10/2026"),
    ("d.m.y", "22.10.2026"),
)
LONG_CHOICES = (
    ("day-mon-d-y", "Thursday, October 22 2026"),
    ("day-d-mon-y", "Thursday, 22 October 2026"),
    ("day-iso", "Thursday 2026-10-22"),
)
TIME_CHOICES = (("24", "17:30"), ("12", "5:30pm"))
WEEK_HEADER_CHOICES = (
    ("ddd m/d", "Thu 10/22"),
    ("ddd d/m", "Thu 22/10"),
    ("ddd mm-dd", "Thu 10-22"),
    ("ddd", "Thu"),
)

# The current choices; loaded at start and whenever Settings, UI is saved.
DISPLAY = {
    "short": "iso",
    "long": "day-mon-d-y",
    "time": "24",
    "week": "ddd m/d",
    "relative": False,
}


def _plain(d: date | datetime) -> date:
    return d.date() if isinstance(d, datetime) else d


def short_date(d, style: str | None = None, today: date | None = None) -> str:
    if d is None:
        return ""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return d
    d = _plain(d)
    if today is not None and DISPLAY.get("relative"):
        delta = (d - today).days
        if delta in (-1, 0, 1):
            return ("Yesterday", "Today", "Tomorrow")[delta + 1]
    style = style or DISPLAY["short"]
    mon = MONTHS[d.month - 1][:3]
    return {
        "mon-d-y": f"{mon} {d.day} {d.year}",
        "d-mon-y": f"{d.day} {mon} {d.year}",
        "m/d/y": f"{d.month:02d}/{d.day:02d}/{d.year}",
        "d/m/y": f"{d.day:02d}/{d.month:02d}/{d.year}",
        "d.m.y": f"{d.day:02d}.{d.month:02d}.{d.year}",
    }.get(style, d.isoformat())


def long_date(d, style: str | None = None) -> str:
    if d is None:
        return ""
    d = _plain(d)
    day, month = DAYS[d.weekday()], MONTHS[d.month - 1]
    return {
        "day-d-mon-y": f"{day}, {d.day} {month} {d.year}",
        "day-iso": f"{day} {d.isoformat()}",
    }.get(style or DISPLAY["long"], f"{day}, {month} {d.day} {d.year}")


def clock(moment: datetime | None, style: str | None = None) -> str:
    if moment is None:
        return ""
    if (style or DISPLAY["time"]) == "12":
        hour = moment.hour % 12 or 12
        return f"{hour}:{moment.minute:02d}{'am' if moment.hour < 12 else 'pm'}"
    return f"{moment.hour:02d}:{moment.minute:02d}"


def week_header(d: date, style: str | None = None) -> str:
    d = _plain(d)
    name = DAYS[d.weekday()][:3]
    return {
        "ddd d/m": f"{name} {d.day}/{d.month}",
        "ddd mm-dd": f"{name} {d.month:02d}-{d.day:02d}",
        "ddd": name,
    }.get(style or DISPLAY["week"], f"{name} {d.month}/{d.day}")


def load(session) -> None:
    from periodicool.library import get_setting

    DISPLAY.update(
        short=get_setting(session, "ui.short_date", "iso"),
        long=get_setting(session, "ui.long_date", "day-mon-d-y"),
        time=str(get_setting(session, "ui.time_format", "24")),
        week=get_setting(session, "ui.week_header", "ddd m/d"),
        relative=bool(get_setting(session, "ui.relative_dates", False)),
    )
