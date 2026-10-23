import io
import re
from pathlib import Path

from periodicool.cli import main
from periodicool.config import load_settings

EXAMPLES = str(Path(__file__).parent.parent / "examples" / "magazines.json")


def run(settings, *argv) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), settings=settings, out=out)
    return code, out.getvalue()


def test_end_to_end(tmp_path):
    lib = tmp_path / "library"
    (lib / "random").mkdir(parents=True)
    (lib / "random" / "MAKE Magazine vol 05.pdf").write_bytes(b"x")
    (lib / "scan0042.pdf").write_bytes(b"y")
    settings = load_settings(
        {"PERIODICOOL_CONFIG_DIR": str(tmp_path / "config"), "PERIODICOOL_LIBRARY_DIR": str(lib)}
    )

    code, out = run(settings, "magazines", "import", EXAMPLES)
    assert code == 0 and out.count("added:") == 8
    code, out = run(settings, "magazines", "import", EXAMPLES)
    assert out.count("exists, unchanged") == 8
    code, out = run(settings, "magazines", "list")
    assert "Fortean Times  (thirteen_per_year, issue)" in out

    code, out = run(settings, "scan")
    assert code == 0 and "Preview only. Nothing was moved." in out
    assert "-> Make/Make - Vol 05.pdf" in out and "REVIEW  scan0042.pdf  [no_magazine]" in out
    assert (lib / "random" / "MAKE Magazine vol 05.pdf").exists()
    scan_id = re.search(r"Scan (\d+)", out).group(1)

    code, out = run(settings, "apply", scan_id)
    assert code == 0 and "moved 1" in out
    assert (lib / "Make" / "Make - Vol 05.pdf").exists()

    code, out = run(settings, "queue")
    item_id = re.search(r"^\s*(\d+)\s+\[no_magazine\]", out, re.M).group(1)
    code, out = run(settings, "resolve", item_id, "999", "v6")
    assert code == 1 and "not resolved" in out
    code, out = run(settings, "skip", item_id)
    assert "skipped" in out
    code, out = run(settings, "queue")
    assert out == ""


def test_scan_rejects_missing_folder(tmp_path):
    settings = load_settings({"PERIODICOOL_CONFIG_DIR": str(tmp_path / "config")})
    code, out = run(settings, "scan", str(tmp_path / "nope"))
    assert code == 2 and "not a folder" in out


def test_indexer_and_client_entries(tmp_path, monkeypatch):
    from periodicool import cli

    settings = load_settings({"PERIODICOOL_CONFIG_DIR": str(tmp_path / "config")})
    monkeypatch.setenv("TEST_NZ_KEY", "cli-key-from-env")
    code, out = run(
        settings,
        "indexer",
        "add",
        "newznab",
        "Direct",
        "https://nzb.example/api",
        "--api-key-env",
        "TEST_NZ_KEY",
    )
    assert code == 0 and "added indexer 1: Direct (newznab)" in out
    monkeypatch.setattr(cli, "KEY_PROMPT", lambda: "typed-key-123")
    code, out = run(
        settings, "client", "add", "sabnzbd", "SAB", "http://sab:8080", "--category", "mags"
    )
    assert code == 0 and "added client 1" in out
    code, out = run(settings, "indexer", "list")
    assert "Direct (newznab) https://nzb.example/api" in out and "key set" in out
    assert "cli-key-from-env" not in out
    code, out = run(settings, "client", "list")
    assert "category 'mags'" in out and "typed-key-123" not in out
    code, out = run(settings, "indexer", "add", "prowlarr", "Bad", "http://u:p@host")
    assert code == 2 and "Leave keys" in out
    code, out = run(settings, "config", "set", "prowlarr.api_key", "x")
    assert code == 2 and "indexer add" in out
    code, out = run(settings, "indexer", "remove", "1")
    assert code == 0 and "removed indexer 1" in out
    assert "no indexer entries" in run(settings, "indexer", "list")[1]


def test_client_add_splits_the_address_and_keeps_a_password(tmp_path, monkeypatch):
    from sqlalchemy import select

    from periodicool import cli, db
    from periodicool.models import DownloadClient

    settings = load_settings({"PERIODICOOL_CONFIG_DIR": str(tmp_path / "config")})
    monkeypatch.setattr(cli, "KEY_PROMPT", lambda: "typed-pass-123")
    code, out = run(
        settings,
        "client",
        "add",
        "deluge",
        "Deluge",
        "https://deluge.example:8112/web",
        "--category",
        "mags",
    )
    assert code == 0 and "typed-pass-123" not in out
    session = db.make_session_factory(db.make_engine(settings.db_url))()
    row = session.scalars(select(DownloadClient)).one()
    assert row.password == "typed-pass-123" and row.api_key is None
    assert row.settings["host"] == "deluge.example" and row.settings["port"] == 8112
    assert row.settings["use_ssl"] is True and row.settings["url_base"] == "/web"
    assert row.url == "https://deluge.example:8112/web"
    assert "key set" in run(settings, "client", "list")[1]
    session.close()
