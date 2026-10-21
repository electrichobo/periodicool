"""Decide whether a parsed release belongs to a magazine.

Matching is strict: the parsed title must equal the magazine title or an alias
once both are normalized. One exception, unless the magazine turns it off:
a special may start with the title and add its own name ("Fortean Times
Paranormal Handbook").
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
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


# Title index: find tracked titles inside a name, so the parser reads the title
# first and takes issue clues only from the rest.

_TOKEN = re.compile(r"&|[^\W_]+(?:['’][^\W_]+)*")


def _fold(word: str) -> str:
    if word == "&":
        return "and"
    folded = unicodedata.normalize("NFKD", word)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    return folded.replace("'", "").replace("’", "")


class TitleIndex:
    """The tracked titles and aliases, as normalized word sequences."""

    def __init__(self, specs: Iterable[TitleSpec]) -> None:
        entries = set()
        for spec in specs:
            for text in (spec.title, *spec.aliases):
                words = tuple(normalize_title(text, spec.filler_optional).split())
                if words:
                    entries.add((words, spec.filler_optional))
        # Longest first, so "Linux Pro Magazine" wins over "Linux".
        self.entries = sorted(entries, key=lambda e: -len(e[0]))

    @classmethod
    def from_magazines(cls, magazines) -> TitleIndex:
        return cls(m.title_spec() for m in magazines)

    def _match_at(self, tokens, i: int) -> int | None:
        """Index just past the longest title starting at token i, or None."""
        for words, filler_optional in self.entries:
            j = i
            for word in words:
                while filler_optional and j < len(tokens) and tokens[j][0] in _FILLER:
                    j += 1
                if j >= len(tokens) or tokens[j][0] != word:
                    break
                j += 1
            else:
                return j
        return None

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Character spans of every tracked title in the text, left to right."""
        tokens = [(_fold(m.group()), m.start(), m.end()) for m in _TOKEN.finditer(text)]
        out, i = [], 0
        while i < len(tokens):
            j = self._match_at(tokens, i)
            if j is None:
                i += 1
                continue
            out.append((tokens[i][1], tokens[j - 1][2]))
            i = j
        return out
