"""Opt-in metadata lookups: Library of Congress, Wikidata and the Internet Archive.

Every source is off until the user turns it on. Only an ISSN or a title is sent.
Results are cached in the database and only suggest or confirm; the user applies
any change. Nothing but metadata is ever fetched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

SOURCES = {
    "loc": "Library of Congress",
    "wikidata": "Wikidata",
    "archive": "Internet Archive",
}
SETTING = "lookups.{}"  # lookups.loc, lookups.wikidata, lookups.archive
NOTICE_KEY = "lookups.notice_dismissed"


class LookupError(Exception):  # noqa: A001  local name, never mixed with the builtin
    pass


@dataclass
class Record:
    """What one source says about a magazine. Everything is optional."""

    source: str
    title: str | None = None
    alt_titles: list[str] = field(default_factory=list)
    issns: list[str] = field(default_factory=list)
    publisher: str | None = None
    frequency_text: str | None = None
    former_frequency_text: str | None = None
    frequency: str | None = None  # a periodicool Frequency value when the words are clear
    start: str | None = None
    start_year: int | None = None
    language: str | None = None
    country: str | None = None
    website: str | None = None
    ref: str | None = None  # the source's own id (LCCN, Q-number)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Record:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def enabled(session, source: str) -> bool:
    from periodicool.library import get_setting

    return bool(get_setting(session, SETTING.format(source), False))


def any_enabled(session) -> bool:
    return any(enabled(session, s) for s in SOURCES)
