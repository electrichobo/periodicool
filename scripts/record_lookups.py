"""Record real metadata lookup responses for the tests (2600 only).

Run by hand when the sources change: python scripts/record_lookups.py
Writes tests/fixtures/lookups/recorded.json. Library of Congress records are US
government public domain, Wikidata is CC0, and only Internet Archive item titles
and dates are kept.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from periodicool.lookups import archive, http, loc, wikidata  # noqa: E402

from fake_lookups import RECORDED, key_for  # noqa: E402

recorded: dict[str, str] = {}


def recording(url, headers, timeout):
    status, body = http._urllib_transport(url, headers, timeout)
    if status == 200:
        recorded[key_for(url)] = body.decode("utf-8")
    return status, body


def main() -> None:
    loc.lookup(issn="0749-3851", transport=recording)
    loc.lookup(title="2600", transport=recording)
    wikidata.lookup(issn="0749-3851", transport=recording)
    wikidata.lookup(title="2600: The Hacker Quarterly", transport=recording)
    archive.search("2600: The Hacker Quarterly", transport=recording)
    RECORDED.parent.mkdir(parents=True, exist_ok=True)
    RECORDED.write_text(json.dumps(recorded, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(recorded)} responses to {RECORDED}")
    for key in sorted(recorded):
        print(" ", key[:100], len(recorded[key]))


if __name__ == "__main__":
    main()
