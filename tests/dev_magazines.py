"""Configs for the dev test magazines, keyed by fixture slug.

Built as in-memory Magazine objects (never saved). Anchors come from release
names in tests/fixtures/releases.
"""

from datetime import date

from periodicool.models import Frequency, Magazine, MonitorMode, Numbering


def _mag(title: str, frequency: Frequency, numbering: Numbering, **kw) -> Magazine:
    defaults = {
        "aliases": [],
        "languages": ["english"],
        "filler_optional": True,
        "special_prefix": True,
        "grace_days": 14,
        "monitored": True,
        "monitor_mode": MonitorMode.NEW_ONLY,
    }
    return Magazine(title=title, frequency=frequency, numbering=numbering, **(defaults | kw))


AMERICAN_CINEMATOGRAPHER = _mag("American Cinematographer", Frequency.MONTHLY, Numbering.DATE)
AMAZONAS = _mag(
    "AMAZONAS Magazine",
    Frequency.BIMONTHLY,
    Numbering.DATE,
    anchor_date=date(2013, 1, 1),  # January/February 2013
)
HACKER_QUARTERLY = _mag("2600: The Hacker Quarterly", Frequency.SEASONAL, Numbering.SEASON)
# The North American edition appears as "Linux Magazine USA", which parses to
# region USA, so no alias is needed and any region is accepted.
LINUX_MAGAZINE = _mag(
    "Linux Magazine",
    Frequency.MONTHLY,
    Numbering.ISSUE,
    filler_optional=False,
    anchor_date=date(2026, 9, 1),  # Issue 310, September 2026
    anchor_number=310,
)
MAKE = _mag("Make:", Frequency.IRREGULAR, Numbering.VOLUME)
FORTEAN_TIMES = _mag(
    "Fortean Times",
    Frequency.THIRTEEN_PER_YEAR,
    Numbering.ISSUE,
    anchor_date=date(2023, 11, 1),  # Issue 437, November 2023 (checks numbers)
    anchor_number=437,
)
TABLETOP_GAMING = _mag(
    "Tabletop Gaming Magazine",
    Frequency.MONTHLY,
    Numbering.ISSUE,
    anchor_date=date(2024, 2, 1),  # Issue 87, 2024-02
    anchor_number=87,
    schedule_start=date(2018, 1, 1),  # seasonal and bimonthly before that
)
PEOPLE = _mag(
    "People",
    Frequency.WEEKLY,
    Numbering.DATE,
    region="USA",
    special_prefix=False,
    anchor_date=date(2023, 7, 10),  # a Monday issue
    publish_weekday=0,
)

BY_FIXTURE = {
    "american-cinematographer": AMERICAN_CINEMATOGRAPHER,
    "amazonas": AMAZONAS,
    "2600": HACKER_QUARTERLY,
    "linux-magazine": LINUX_MAGAZINE,
    "linux-pro-magazine": LINUX_MAGAZINE,
    "make": MAKE,
    "fortean-times": FORTEAN_TIMES,
    "tabletop-gaming": TABLETOP_GAMING,
    "people": PEOPLE,
}
