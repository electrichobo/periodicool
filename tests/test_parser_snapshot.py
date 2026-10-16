"""Every fixture name, parsed and matched, compared to a reviewed snapshot.

If this fails after a deliberate parser change, run
`python scripts/update_parser_snapshot.py` and review the diff of
tests/fixtures/parser_snapshot.json line by line before committing.
"""

import json

from snapshot import SNAPSHOT_PATH, build_snapshot


def test_snapshot_matches():
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    actual = build_snapshot()
    changed = [
        (slug, name)
        for slug in sorted(set(expected) | set(actual))
        for name in sorted(set(expected.get(slug, {})) | set(actual.get(slug, {})))
        if expected.get(slug, {}).get(name) != actual.get(slug, {}).get(name)
    ]
    assert not changed, f"{len(changed)} parses changed, first: {changed[:5]}"
