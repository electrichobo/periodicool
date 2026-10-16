"""Regenerate tests/fixtures/parser_snapshot.json from the release fixtures.

Run after a deliberate parser change, then review the diff before committing:
every changed line is a changed parse.

    python scripts/update_parser_snapshot.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from snapshot import SNAPSHOT_PATH, build_snapshot, dump_snapshot  # noqa: E402

if __name__ == "__main__":
    SNAPSHOT_PATH.write_text(dump_snapshot(build_snapshot()), encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH.relative_to(ROOT)}")
