"""Theme checks: WCAG AA contrast of every text pair, and assets in the package."""

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
THEME = ROOT / "src" / "periodicool" / "static" / "css" / "theme.css"


def tokens() -> dict[str, str]:
    root = re.search(r":root\s*{(.*?)}", THEME.read_text(encoding="utf-8"), re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", root))


def luminance(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT = 4.5  # normal text
UI = 3.0  # large text, focus rings, control borders

# (foreground, background, minimum). Names are tokens; literals are colors used directly.
PAIRS = [
    ("on-chassis", "chassis", TEXT),
    ("on-chassis-muted", "chassis", TEXT),
    ("on-chassis", "chassis-raised", TEXT),
    ("on-chassis-muted", "chassis-raised", TEXT),
    ("ink", "panel", TEXT),
    ("ink-muted", "panel", TEXT),
    ("ink", "panel-recess", TEXT),
    ("ink-muted", "panel-recess", TEXT),
    ("amber-ink", "panel", TEXT),
    ("tape-red", "panel", TEXT),  # field error text
    ("ink", "#fbf8f0", TEXT),  # input text
    ("ink", "#d9d0ba", TEXT),  # default button, darkest part of its gradient
    ("on-orange", "orange", TEXT),  # primary button
    ("#ffffff", "#c0362b", TEXT),  # danger button
    ("on-chassis", "#302d29", TEXT),  # chassis button
    ("chassis", "amber", TEXT),  # active nav item
    ("on-orange", "amber", TEXT),  # skip link
    ("crt-fg", "crt-bg", TEXT),
    ("crt-dim", "crt-bg", TEXT),
    ("amber", "#0d0c0b", UI),  # tape counter, large digits
    ("on-tape", "tape-black", TEXT),
    ("on-tape", "tape-red", TEXT),
    ("on-tape", "tape-blue", TEXT),
    ("on-tape-yellow", "tape-yellow", TEXT),
    ("ink", "#e8d9b0", TEXT),  # VU meter face
    ("focus", "panel", UI),
    ("focus", "chassis", UI),
]


@pytest.mark.parametrize(("fg", "bg", "minimum"), PAIRS, ids=lambda v: str(v))
def test_contrast(fg, bg, minimum):
    t = tokens()
    a, b = (t[x] if not x.startswith("#") else x for x in (fg, bg))
    ratio = contrast(a, b)
    assert ratio >= minimum, f"{fg} on {bg}: {ratio:.2f} < {minimum}"


def test_reduced_motion_turns_effects_off():
    css = THEME.read_text(encoding="utf-8")
    block = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert ".crt::after" in block and "display: none" in block
    assert "animation: none" in block


def test_no_external_urls_in_assets():
    """No CDNs: CSS, JS and templates only reference our own /static files."""
    pkg = ROOT / "src" / "periodicool"
    for path in [
        *pkg.glob("static/css/*.css"),
        *pkg.glob("static/js/*.js"),
        *pkg.glob("templates/**/*.html"),
    ]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(src|href|url)\s*[=(]\s*[\"']?https?://", text), path


@pytest.mark.slow
def test_wheel_contains_templates_and_static(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "-q",
            "-w",
            str(tmp_path),
            str(ROOT),
        ],
        check=True,
    )
    [wheel] = tmp_path.glob("periodicool-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for needed in [
        "periodicool/templates/base.html",
        "periodicool/templates/ui_kit.html",
        "periodicool/static/css/theme.css",
        "periodicool/static/js/app.js",
        "periodicool/static/vendor/htmx/htmx.min.js",
        "periodicool/static/vendor/dseg/DSEG7Classic-Regular.woff2",
        "periodicool/static/vendor/ibm-plex-mono/IBMPlexMono-Regular.woff2",
        "periodicool/static/vendor/barlow-condensed/BarlowCondensed-Medium.woff2",
        "periodicool/static/vendor/SOURCES.md",
        "periodicool/migrations/versions/0005_downloads.py",
    ]:
        assert needed in names, needed


def test_ui_kit_page(client):
    resp = client.get("/ui-kit")
    assert resp.status_code == 200
    html = resp.text
    assert 'href="/static/css/theme.css' in html and "htmx.min.js" in html
    assert "Skip to content" in html and 'id="main"' in html
    assert client.get("/static/vendor/htmx/htmx.min.js").status_code == 200
    font = client.get("/static/vendor/dseg/DSEG7Classic-Regular.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"


def test_ui_kit_shows_shared_components(client):
    html = client.get("/ui-kit").text
    for marker in [
        'class="toolbar"',
        'class="table" id="kit-mags"',
        'class="bulkbar"',
        'class="fold"',
        'class="pager"',
        'class="chart"',
        'role="progressbar"',
        'data-confirm-type="clear"',
        'id="modal-body"',
        'id="confirm"',
        'id="toasts"',
    ]:
        assert marker in html, marker
    # Static URLs carry a stamp that changes when the file changes.
    assert "/static/css/theme.css?v=" in html


def test_ui_kit_modal_and_toast(client):
    modal = client.get("/ui-kit/modal")
    assert (
        modal.status_code == 200
        and 'id="modal-title"' in modal.text
        and 'role="tablist"' in modal.text
    )
    toast = client.post("/ui-kit/toast")
    assert json.loads(toast.headers["HX-Trigger"])["toast"]["kind"] == "ok"
