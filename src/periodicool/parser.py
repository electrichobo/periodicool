"""Release and file name parser.

Pulls the magazine title and issue identity out of a release or file name
without knowing which magazine it belongs to. Matching a parsed name to a
magazine happens in `periodicool.matching`.

The parser never guesses. Anything ambiguous or left over adds a reason, and
any reason makes the result low confidence.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

HIGH = "high"
LOW = "low"

_MONTH_NAMES = {
    1: ("january", "januar", "jan"),
    2: ("february", "februar", "feb"),
    3: ("march", "märz", "maerz", "mar"),
    4: ("april", "apr"),
    5: ("may", "mai"),
    6: ("june", "juni", "jun"),
    7: ("july", "juli", "jul"),
    8: ("august", "aug"),
    9: ("september", "sept", "sep"),
    10: ("october", "oktober", "oct", "okt"),
    11: ("november", "nov"),
    12: ("december", "dezember", "dec", "dez"),
}
MONTHS = {name: num for num, names in _MONTH_NAMES.items() for name in names}
SEASONS = {
    "spring": "spring",
    "summer": "summer",
    "autumn": "autumn",
    "fall": "autumn",
    "winter": "winter",
}
LANGUAGES = {
    "english": "english",
    "german": "german",
    "deutsch": "german",
    "italian": "italian",
    "dutch": "dutch",
    "french": "french",
    "spanish": "spanish",
    "polish": "polish",
    "russian": "russian",
}
# Region phrases recognized inside a title (never as its first word).
REGIONS = {
    "south africa": "South Africa",
    "new zealand": "New Zealand",
    "australia": "Australia",
    "usa": "USA",
    "uk": "UK",
    "canada": "Canada",
    "india": "India",
    "ireland": "Ireland",
    "singapore": "Singapore",
    "malaysia": "Malaysia",
    "philippines": "Philippines",
    "international": "International",
}
FORMATS = ("pdf", "epub", "cbz", "cbr")

_I = re.IGNORECASE
_MONTH = "(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + ")"
_SEASON = "(?:" + "|".join(SEASONS) + ")"
_YEAR = r"(?:19|20)\d{2}"

# Junk around the real name: post subjects, sizes, file parts, spam tags.
_QUOTED = re.compile(r'"([^"]+)"?')
_DOUBLE_BRACKET = re.compile(r"^\[\[\s*(.+?)\s*\]\]")
_LEAD_DASH = re.compile(r"^[\s\-]+")
_LEAD_COUNTER = re.compile(r"^\d{1,4}\s*-\s*(?=[A-Za-z\[])")
_LEAD_TAG = re.compile(r"^\[?MAGAZINE[\])]?[\s.]*")
_SIZE_TAIL = re.compile(r"\s+-\s+\d+(?:[.,]\d+)?\s*(?:KB|MB|GB)\b.*$", _I)
_PART_TAIL = re.compile(r"\s+\d+\s+of\s+\d+\s.*$", _I)
_COUNTER_TAIL = re.compile(r"\s*-?\s*\(\d+/\d+\)\s*$")
_NUMBER_TAIL = re.compile(r"\s+-\s+\d+\s*$")
_FILE_EXT = (
    r"(?:pdf|epub|cbz|cbr|par2|rar|r\d\d|sfv|nfo|jpe?g|png|zip|nzb"
    r"|vol\d+(?:\+\d+)?|part\d+)"
)
_EXT_CHAIN = re.compile(r"(?:\." + _FILE_EXT + r")+(?:\s*-?\s*yEnc)?\s*$", _I)
_YENC = re.compile(r"[\s\-]*yEnc\s*$", _I)
_SPAM_TAG = re.compile(r"[\s.]*-[\s_]*[A-Z0-9]+(?:\.[A-Z0-9]+)+[\s_]*-\s*$")
_FIRST_TOKEN = re.compile(r"[\s._]+")

# Markers. They end the title but carry no issue identity.
_TRUE_PDF = re.compile(r"\btrue\s*pdf\b", _I)
_GROUP = re.compile(r"\b(?:e-?book|magazine|read|retail)-([A-Za-z0-9]+)\s*$", _I)
_QUALITY = re.compile(r"\b(hybrid|retail|proper|hq|scan(?:ned)?|e-?book)\b", _I)
_FORMAT = re.compile(r"\b(" + "|".join(FORMATS) + r")\b", _I)
_LANGUAGE = re.compile(r"\b(" + "|".join(LANGUAGES) + r")\b", _I)
_PACK = re.compile(
    r"\b(mega\s*pack|pack|complete(?=\W.*\bcollection\b)|collection|full\s+year|bundle)\b", _I
)
_SPECIAL = re.compile(
    r"\b(specials?(?:\s+editions?)?|yearbook|annual|handbook|bookazines?"
    r"|collector'?s\s+edition)\b",
    _I,
)

# Dates and numbers, in the order they are tried.
_SEASON_YEAR = re.compile(rf"\b({_SEASON})[\s\-,]+({_YEAR})\b", _I)
_DAY_MONTH_YEAR = re.compile(
    rf"\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[\s\-]+({_MONTH})\b\.?[\s\-,]+({_YEAR})\b", _I
)
_MONTH_RANGE_YEAR = re.compile(
    rf"\b({_MONTH})\b\.?\s*[\-/&]?\s*({_MONTH})\b\.?[\s\-,]+({_YEAR})\b", _I
)
_MONTH_YEAR = re.compile(rf"\b({_MONTH})\b\.?[\s\-,]+({_YEAR})\b", _I)
_ISO_YMD = re.compile(rf"\b({_YEAR})[\s\-](0[1-9]|1[0-2])[\s\-](0[1-9]|[12]\d|3[01])\b")
_ISO_YM = re.compile(rf"\b({_YEAR})[\s\-](0[1-9]|1[0-2])\b(?![\s\-]\d)")
_ISSUE = re.compile(
    r"(?:\b(?:issue|no|nr|number)\b\.?\s*|#\s*)(\d{1,5})\b"
    r"(?:\s*(?:&|-|/|and)\s*(\d{1,3})\b)?",
    _I,
)
_VOLUME = re.compile(r"\bvol(?:ume)?s?\b\.?\s*(\d{1,4})\b(?:\s*-\s*(\d{1,4})\b)?", _I)
_VOLUME_GLUED = re.compile(r"\bvol(?:ume)?(\d{1,4})\b", _I)
_WEEK = re.compile(r"\b(?:week|wk)\s*(\d{1,2})\b", _I)
_NUMERIC_DMY = re.compile(rf"\b(\d{{1,2}})[\s\-](\d{{1,2}})[\s\-]({_YEAR})\b")
_NUMERIC_MY = re.compile(rf"\b(0?[1-9]|1[0-2])[\s\-/]({_YEAR})\b")
_RANGE = re.compile(r"\b(\d{1,3})\s*-\s*(\d{1,3})\b")
_LONE_MONTH = re.compile(rf"\b({_MONTH})\b", _I)
_LONE_SEASON = re.compile(rf"\b({_SEASON})\b", _I)
_ANY_YEAR = re.compile(rf"\b({_YEAR})\b")
_DIGITS = re.compile(r"\d+")
_DANGLING = re.compile(r"(?:\s+(?:vol(?:ume)?|issue|no|nr|number))+$", _I)
_EDGE_JUNK = re.compile(r"^[\W_]+|[\W_]+$")

_MASK = "\x00"


@dataclass(frozen=True)
class ParsedRelease:
    raw: str
    title: str
    region: str | None = None
    language: str | None = None
    year: int | None = None
    month: int | None = None
    month_end: int | None = None
    day: int | None = None
    season: str | None = None
    week: int | None = None
    issue: int | None = None
    issue_end: int | None = None
    volume: int | None = None
    volume_end: int | None = None
    special: bool = False
    pack: bool = False
    true_pdf: bool = False
    formats: tuple[str, ...] = ()
    quality: tuple[str, ...] = ()
    group: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def confidence(self) -> str:
        return LOW if self.reasons else HIGH


def _first_token(s: str) -> str:
    return _FIRST_TOKEN.split(s.strip(), maxsplit=1)[0].lower()


def _drop_repeated_tail(s: str) -> str:
    """Drop a ' - <same name again>' tail, as in 'X Vol 01 - X vol 01.par2'."""
    first = _first_token(s)
    for m in re.finditer(r"\s+-\s+", s):
        if first and _first_token(s[m.end() :]) == first:
            return s[: m.start()]
    return s


def clean_name(raw: str) -> tuple[str, tuple[str, ...]]:
    """Strip post-subject junk. Returns the cleaned name and file formats seen."""
    s = html.unescape(raw).strip()
    if m := _QUOTED.search(s):
        s = m.group(1)
    if m := _DOUBLE_BRACKET.match(s):
        s = m.group(1)
    s = _LEAD_DASH.sub("", s)
    s = _LEAD_COUNTER.sub("", s)
    s = _LEAD_TAG.sub("", s)
    s = _LEAD_DASH.sub("", s)
    for pattern in (_SIZE_TAIL, _PART_TAIL, _COUNTER_TAIL, _NUMBER_TAIL):
        s = pattern.sub("", s)
    s = _drop_repeated_tail(s)
    formats: list[str] = []
    if m := _EXT_CHAIN.search(s):
        formats = [e for e in re.findall(r"\.(\w+)", m.group(0).lower()) if e in FORMATS]
        s = s[: m.start()]
    s = _YENC.sub("", s)
    s = _SPAM_TAG.sub("", s)
    s = s.replace("_", " ").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s, tuple(dict.fromkeys(formats))


class _Scanner:
    """Finds patterns in a string and masks each match so it is used only once."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts: list[int] = []

    def take(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        m = pattern.search(self.text)
        if m:
            self.starts.append(m.start())
            masked = _MASK * (m.end() - m.start())
            self.text = self.text[: m.start()] + masked + self.text[m.end() :]
        return m

    def take_all(self, pattern: re.Pattern[str]) -> list[re.Match[str]]:
        found = []
        while m := self.take(pattern):
            found.append(m)
        return found


def _split_region(title: str) -> tuple[str, str | None]:
    """Pull a region phrase out of a title, unless it is the first word."""
    words = title.split(" ")
    lowered = [w.lower() for w in words]
    for phrase, name in sorted(REGIONS.items(), key=lambda kv: -len(kv[0])):
        parts = phrase.split(" ")
        for i in range(1, len(words) - len(parts) + 1):
            if lowered[i : i + len(parts)] == parts:
                return " ".join(words[:i] + words[i + len(parts) :]), name
    for i in range(1, len(words)):
        if words[i] == "US":  # case-sensitive: "us" is an English word
            return " ".join(words[:i] + words[i + 1 :]), "USA"
    return title, None


def parse_release(raw: str) -> ParsedRelease:
    text, file_formats = clean_name(raw)
    sc = _Scanner(text)
    found: dict = {}
    formats = list(file_formats)
    reasons: list[str] = []

    true_pdf = bool(sc.take_all(_TRUE_PDF))
    if true_pdf:
        formats.append("pdf")
    if m := sc.take(_GROUP):
        found["group"] = m.group(1)
    quality = [m.group(1).lower().replace("-", "") for m in sc.take_all(_QUALITY)]
    quality = ["scan" if q.startswith("scan") else q for q in quality if q != "ebook"]
    formats += [m.group(1).lower() for m in sc.take_all(_FORMAT)]
    languages = [LANGUAGES[m.group(1).lower()] for m in sc.take_all(_LANGUAGE)]
    pack = bool(sc.take_all(_PACK))
    special = bool(sc.take_all(_SPECIAL))

    year: int | None = None
    if m := sc.take(_SEASON_YEAR):
        found["season"] = SEASONS[m.group(1).lower()]
        year = int(m.group(2))
    elif m := sc.take(_DAY_MONTH_YEAR):
        found["day"], found["month"] = int(m.group(1)), MONTHS[m.group(2).lower()]
        year = int(m.group(3))
    elif m := sc.take(_MONTH_RANGE_YEAR):
        found["month"] = MONTHS[m.group(1).lower()]
        found["month_end"] = MONTHS[m.group(2).lower()]
        year = int(m.group(3))
    elif m := sc.take(_MONTH_YEAR):
        found["month"], year = MONTHS[m.group(1).lower()], int(m.group(2))
    elif m := sc.take(_ISO_YMD):
        year, found["month"], found["day"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := sc.take(_ISO_YM):
        year, found["month"] = int(m.group(1)), int(m.group(2))

    if m := sc.take(_ISSUE):
        found["issue"] = int(m.group(1))
        if m.group(2) and 0 < int(m.group(2)) - int(m.group(1)) <= 60:
            found["issue_end"] = int(m.group(2))
    if m := sc.take(_VOLUME):
        found["volume"] = int(m.group(1))
        if m.group(2) and int(m.group(2)) > int(m.group(1)):
            found["volume_end"] = int(m.group(2))
    elif m := sc.take(_VOLUME_GLUED):
        found["volume"] = int(m.group(1))
    if m := sc.take(_WEEK):
        found["week"] = int(m.group(1))

    if year is None and (m := sc.take(_NUMERIC_DMY)):
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 and 1 <= b <= 12:
            found["day"], found["month"] = a, b
        elif b > 12 and 1 <= a <= 12:
            found["month"], found["day"] = a, b
        else:
            reasons.append(f"ambiguous numeric date {m.group(0)!r}")
    if year is None and (m := sc.take(_NUMERIC_MY)):
        found["month"], year = int(m.group(1)), int(m.group(2))
    if "issue" not in found and (m := sc.take(_RANGE)):
        start, end = int(m.group(1)), int(m.group(2))
        if end > start:
            found["issue"], found["issue_end"] = start, end

    if "month" not in found and (m := sc.take(_LONE_MONTH)) and year is None:
        reasons.append(f"month without year {m.group(1)!r}")
    if "season" not in found and (m := sc.take(_LONE_SEASON)) and year is None:
        reasons.append(f"season without year {m.group(1)!r}")

    years = {int(m.group(1)) for m in sc.take_all(_ANY_YEAR)}
    if year is not None:
        years.add(year)
    if len(years) == 1:
        year = next(iter(years))
    elif len(years) > 1:
        reasons.append("conflicting years " + ", ".join(str(y) for y in sorted(years)))

    # The title runs up to the first marker, date or bracket, or the first
    # number after the first word.
    end = min(sc.starts, default=len(text))
    first_word_end = text.find(" ") if " " in text else len(text)
    for m in _DIGITS.finditer(sc.text):
        if m.start() >= first_word_end:
            end = min(end, m.start())
            break
    for bracket in ("[", "("):
        pos = text.find(bracket, 1)
        if pos != -1:
            end = min(end, pos)
    title = _EDGE_JUNK.sub("", text[:end])
    title = _EDGE_JUNK.sub("", _DANGLING.sub("", title))
    title, region = _split_region(re.sub(r"\s+", " ", title))

    leftovers = [m.group(0) for m in _DIGITS.finditer(sc.text) if m.start() >= end]
    if leftovers:
        reasons.append("unrecognized numbers " + " ".join(leftovers))

    issue_end, volume_end = found.get("issue_end"), found.get("volume_end")
    if (issue_end and issue_end - found["issue"] > 1) or (
        volume_end and volume_end - found["volume"] > 1
    ):
        pack = True
    if pack:
        reasons.append("multi-issue pack")
    if not title:
        reasons.append("no title")
    has_date = year is not None and any(k in found for k in ("month", "season", "week"))
    if not (has_date or "issue" in found or "volume" in found):
        reasons.append("no issue identity")
    if len(set(languages)) > 1:
        reasons.append("conflicting languages")

    return ParsedRelease(
        raw=raw,
        title=title,
        region=region,
        language=languages[0] if languages else None,
        year=year,
        month=found.get("month"),
        month_end=found.get("month_end"),
        day=found.get("day"),
        season=found.get("season"),
        week=found.get("week"),
        issue=found.get("issue"),
        issue_end=issue_end,
        volume=found.get("volume"),
        volume_end=volume_end,
        special=special,
        pack=pack,
        true_pdf=true_pdf,
        formats=tuple(dict.fromkeys(formats)),
        quality=tuple(dict.fromkeys(quality)),
        group=found.get("group"),
        reasons=tuple(dict.fromkeys(reasons)),
    )
