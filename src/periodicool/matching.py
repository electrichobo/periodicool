"""Decide whether a parsed release belongs to a magazine.

Matching is strict: the parsed title must equal the magazine title or an alias
once both are normalized. One exception, unless the magazine turns it off:
a special may start with the title and add its own name ("Fortean Times
Paranormal Handbook").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from periodicool.parser import ParsedRelease

# Words ignored when comparing titles, unless the magazine says they matter.
_FILLER = {"magazine", "mag"}


@dataclass(frozen=True)
class TitleSpec:
    title: str
    aliases: tuple[str, ...] = ()
    region: str | None = None  # None accepts any region
    languages: tuple[str, ...] = ("english",)
    # False when "Magazine" is part of the name: "Linux" is not "Linux Magazine".
    filler_optional: bool = True
    # False for generic titles: "People Wellness Special" is not taken as People.
    special_prefix: bool = True


@dataclass(frozen=True)
class MatchResult:
    accepted: bool
    reason: str


def normalize_title(title: str, drop_filler: bool = True) -> str:
    """Lowercase, fold accents, drop punctuation and (optionally) filler words."""
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    folded = folded.replace("&", " and ").replace("'", "")
    words = re.sub(r"[\W_]+", " ", folded).split()
    if drop_filler:
        words = [w for w in words if w not in _FILLER]
    return " ".join(words)


def match_title(parsed: ParsedRelease, spec: TitleSpec) -> MatchResult:
    got = normalize_title(parsed.title, spec.filler_optional)
    if not got:
        return MatchResult(False, "no title")
    wanted = [normalize_title(t, spec.filler_optional) for t in (spec.title, *spec.aliases)]
    if got in wanted:
        how = "title"
    elif (
        parsed.special
        and spec.special_prefix
        and any(w and got.startswith(w + " ") for w in wanted)
    ):
        how = "special"
    else:
        return MatchResult(False, f"title {parsed.title!r} is not {spec.title!r}")

    if spec.region and parsed.region and parsed.region.lower() != spec.region.lower():
        return MatchResult(False, f"region {parsed.region}, wanted {spec.region}")
    if parsed.language and parsed.language not in spec.languages:
        return MatchResult(False, f"language {parsed.language}")
    return MatchResult(True, f"matched {how}")
