"""Print one version's section of CHANGELOG.md, for a GitHub release's notes.

Fails when the version has no section or the section is empty, so a release is
never published without notes.

    python scripts/release_notes.py v1.2.3
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(text: str, version: str) -> str:
    """The body under `## [version]`, up to the next heading or the link list."""
    lines = iter(text.splitlines())
    start = re.compile(rf"## \[{re.escape(version)}\](\s|$)")
    if not any(start.match(line) for line in lines):
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    body = []
    for line in lines:
        if line.startswith("## [") or re.match(r"\[[^\]]+\]: ", line):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md has an empty section for {version}")
    return notes


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        notes = section(CHANGELOG.read_text(encoding="utf-8"), argv[0].removeprefix("v"))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
