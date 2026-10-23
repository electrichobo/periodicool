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


def tokens(mode: str = "light") -> dict[str, str]:
    """Color tokens; dark mode is the light set overridden by the dark block."""
    css = THEME.read_text(encoding="utf-8")
    blocks = [re.search(r":root\s*{(.*?)}", css, re.S).group(1)]
    if mode == "dark":
        blocks.append(re.search(r':root\[data-theme="dark"\]\s*{(.*?)}', css, re.S).group(1))
    found: dict[str, str] = {}
    for block in blocks:
        found |= dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))
    return found


def luminance(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT = 4.5  # normal text
UI = 3.0  # large text, focus rings, control borders

# Lamp faces: the average color of the middle of each lamp tile, where the label sits
# (sampled from static/img/lamps; also the fallback colors in theme.css).
LAMP_OFF = "#572007"
LAMP_ON = "#d27328"
LAMP_BLUE_ON = "#3083bf"
LAMP_GREEN_OFF = "#403916"
LAMP_GREEN_ON = "#82ab42"

# (foreground, background, minimum). Names are tokens; literals are colors used directly.
PAIRS = [
    ("ink", "page", TEXT),
    ("ink", "panel", TEXT),
    ("ink", "panel-recess", TEXT),
    ("ink", "input-bg", TEXT),
    ("ink", "row-selected", TEXT),
    ("ink", "cal-other", TEXT),
    ("ink-label", "page", TEXT),
    ("ink-label", "panel", TEXT),
    ("ink-muted", "page", TEXT),
    ("ink-muted", "panel", TEXT),
    ("ink-muted", "panel-recess", TEXT),
    ("orange-ink", "panel", TEXT),  # link hover, naming tokens
    ("tape-red", "panel", TEXT),  # field error text
    ("on-dark", "dark", TEXT),  # header
    ("on-dark-muted", "dark", TEXT),
    ("green-text", "dark", TEXT),  # console, API key
    ("orange-bright", "dark", UI),  # counter digits, large
    ("counter-dim", "dark", UI),  # "/ 322" on the counter, large
    ("on-lamp", LAMP_OFF, TEXT),  # unlit buttons
    ("on-lamp-lit", LAMP_ON, TEXT),  # lit orange: current, primary
    ("on-lamp-blue", LAMP_BLUE_ON, TEXT),  # lit blue: chosen view
    ("on-lamp", LAMP_GREEN_OFF, TEXT),  # switch off
    ("#10240a", LAMP_GREEN_ON, TEXT),  # switch on
    ("on-tape-yellow", "tape-yellow", TEXT),
    ("#ffffff", "#b3261e", TEXT),  # red tag
    ("#ffffff", "tape-blue", TEXT),  # blue tag
    ("focus", "panel", UI),
    ("focus", "page", UI),
]


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg", "minimum"), PAIRS, ids=lambda v: str(v))
def test_contrast(fg, bg, minimum, mode):
    t = tokens(mode)
    a, b = (t[x] if not x.startswith("#") else x for x in (fg, bg))
    ratio = contrast(a, b)
    assert ratio >= minimum, f"{mode}: {fg} on {bg}: {ratio:.2f} < {minimum}"


def test_dark_mode_follows_the_system_and_the_setting():
    css = THEME.read_text(encoding="utf-8")
    auto = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*{\s*"
        r':root:not\(\[data-theme="light"\]\)\s*{(.*?)}',
        css,
        re.S,
    ).group(1)
    forced = re.search(r':root\[data-theme="dark"\]\s*{(.*?)}', css, re.S).group(1)
    assert auto.split() == forced.split()


def test_lamp_tiles_and_font_are_served(client):
    for name in ("orange-off", "orange-on", "blue-on", "green-off", "green-on"):
        resp = client.get(f"/static/img/lamps/lamp-{name}.png")
        assert resp.status_code == 200 and resp.content[:4] == b"\x89PNG"
    font = client.get("/static/vendor/chelsea-market/ChelseaMarket-Regular.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"


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
