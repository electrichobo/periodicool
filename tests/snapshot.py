"""Parser snapshot over every fixture name, shared by the test and the update script."""

import json
from pathlib import Path

from periodicool.matching import TitleIndex, match_title
from periodicool.parser import parse_release

from dev_magazines import BY_FIXTURE

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_PATH = FIXTURES / "parser_snapshot.json"
# Parsed as the app does: with every dev magazine's title known.
TITLES = TitleIndex.from_magazines(BY_FIXTURE.values())


def summarize(name: str, slug: str) -> dict:
    parsed = parse_release(name, TITLES)
    fields = {
        k: v
        for k, v in vars(parsed).items()
        if k not in ("raw", "reasons") and v not in (None, False, ())
    }
    fields = {k: list(v) if isinstance(v, tuple) else v for k, v in fields.items()}
    fields["confidence"] = parsed.confidence
    if parsed.reasons:
        fields["reasons"] = list(parsed.reasons)
    match = match_title(parsed, BY_FIXTURE[slug].title_spec())
    fields["match"] = match.accepted
    return fields


def build_snapshot() -> dict:
    out = {}
    for path in sorted((FIXTURES / "releases").glob("*.json")):
        releases = json.loads(path.read_text(encoding="utf-8"))["releases"]
        out[path.stem] = {r["title"]: summarize(r["title"], path.stem) for r in releases}
    return out


def dump_snapshot(data: dict) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"
