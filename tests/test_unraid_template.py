"""The Unraid Community Apps template matches the image and holds nothing personal."""

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "unraid" / "periodicool.xml"
ICON = ROOT / "unraid" / "periodicool.png"
# Generic defaults only: no private networks or personal host names in a public file.
FORBIDDEN = ("192.168.", "10.0.", "172.16.", "NASA", "/mnt/user/media/mags")


def _configs():
    root = ET.parse(TEMPLATE).getroot()
    return root, {c.get("Target"): c for c in root.iter("Config")}


def test_required_fields():
    root, _ = _configs()
    assert root.tag == "Container" and root.get("version") == "2"
    for tag in (
        "Name",
        "Repository",
        "Registry",
        "Network",
        "Support",
        "Project",
        "Overview",
        "Category",
        "WebUI",
        "TemplateURL",
        "Icon",
    ):
        assert (root.findtext(tag) or "").strip(), tag
    assert root.findtext("Repository") == "ghcr.io/electrichobo/periodicool:latest"
    assert root.findtext("Privileged") == "false"
    assert "no content and no indexers" in root.findtext("Overview")


def test_matches_the_dockerfile():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    port = re.search(r"^EXPOSE (\d+)", dockerfile, re.M).group(1)
    root, configs = _configs()
    assert f"[PORT:{port}]" in root.findtext("WebUI")
    assert configs[port].get("Type") == "Port" and configs[port].text == port
    for name in ("PUID", "PGID", "UMASK"):
        default = re.search(rf"\b{name}=(\S+)", dockerfile).group(1)
        assert configs[name].get("Default") == default, name
    for path in ("/config", "/library", "/data"):
        assert configs[path].get("Type") == "Path", path
    assert configs["PERIODICOOL_DOWNLOADS_DIR"].text == "/data"
    for key in ("PROWLARR_API_KEY", "SABNZBD_API_KEY"):
        assert configs[key].get("Mask") == "true" and configs[key].get("Required") == "false"


def test_nothing_personal():
    text = TEMPLATE.read_text(encoding="utf-8")
    for word in FORBIDDEN:
        assert word not in text, word
    assert chr(0x2014) not in text  # no em dashes


def test_ca_profile():
    """Community Apps wants ca_profile.xml at the repository root, with a Profile."""
    root = ET.parse(ROOT / "ca_profile.xml").getroot()
    assert root.tag == "CommunityApplications"
    assert len((root.findtext("Profile") or "").strip()) > 100
    assert "no content and no indexers" in root.findtext("Profile")
    for tag in ("Icon", "WebPage", "Forum"):
        assert (root.findtext(tag) or "").startswith("https://"), tag
    text = (ROOT / "ca_profile.xml").read_text(encoding="utf-8")
    for word in FORBIDDEN:
        assert word not in text, word
    assert chr(0x2014) not in text


def test_icon_is_a_square_png():
    data = ICON.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height >= 256
