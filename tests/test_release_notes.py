"""scripts/release_notes.py: the notes a GitHub release is published with."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"
VERSIONS = re.findall(
    r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M
)


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, encoding="utf-8", check=False
    )


def test_section_holds_only_that_version():
    result = run("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("### Fixed")
    assert "date range" in result.stdout
    assert "0.3.0" not in result.stdout and "Plain" not in result.stdout


def test_tag_prefix_is_optional():
    assert run("0.3.1").stdout == run("v0.3.1").stdout


@pytest.mark.parametrize("version", VERSIONS)
def test_every_released_version_has_notes(version):
    result = run(version)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    assert "## [" not in result.stdout
    assert not re.search(r"^\[[^\]]+\]: ", result.stdout, re.M)


def test_unknown_version_fails():
    result = run("v99.0.0")
    assert result.returncode == 1
    assert "no section" in result.stderr
    assert not result.stdout


def test_similar_version_is_not_matched():
    assert run("0.3").returncode == 1


def test_empty_section_fails():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        from release_notes import section
    finally:
        sys.path.pop(0)
    with pytest.raises(ValueError, match="empty"):
        section("## [1.0.0] - 2026-01-01\n\n## [0.9.0] - 2025-01-01\n\n- Old\n", "1.0.0")
