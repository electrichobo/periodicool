"""ISSN, the release window, and the quick PDF read in library scans and imports."""

import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from periodicool import downloads, library, pdf_text
from periodicool.decisions import evaluate
from periodicool.issn import check_digit, normalize
from periodicool.library import plan_scan
from periodicool.models import (
    GrabStatus,
    History,
    HistoryEvent,
    ImportItem,
    ItemAction,
    Magazine,
    ScanKind,
)
from periodicool.pdf_text import clues_from_text, read_clues
from periodicool.search import wanted_issues

from pdfgen import make_pdf
from test_downloads import L311, TODAY, env, finish, seen  # noqa: F401
from test_library import lib, session  # noqa: F401

# Valid by check digit; test values, not claims about real magazines.
ISSN_2600 = "0749-385" + check_digit("0749385")
ISSN_AMAZONAS_DE = "2194-401" + check_digit("2194401")
ISSN_LINUX = "1471-567" + check_digit("1471567")


def mag(s, title):
    return s.scalars(select(Magazine).where(Magazine.title == title)).one()


# ISSN


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("07493851", "0749-3851"),
        (" 0749 3851 ", "0749-3851"),
        ("2434-561x", "2434-561X"),
        ("0749-3852", None),
        ("1234", None),
        ("", None),
        (None, None),
    ],
)
def test_issn_normalize(raw, want):
    assert normalize(raw) == want


def test_issn_in_text():
    text = "Printed in USA. ISSN (Print): 0749 3851. ISSN 1234-5678 is wrong. ISSN0317-8471"
    assert clues_from_text(text).issns == ("0749-3851", "0317-8471")


# Reading PDFs


def test_read_clues_from_a_pdf(tmp_path):
    path = make_pdf(tmp_path / "x.pdf", ["2600", "Autumn 2026", f"ISSN {ISSN_2600}"])
    clues = read_clues(path)
    assert clues.issns == (ISSN_2600,) and clues.season == "autumn" and clues.year == 2026
    assert "Autumn 2026" in clues.summary()


@pytest.mark.parametrize("content", [b"not a pdf at all", b"%PDF-1.4\n%%EOF", b""])
def test_unreadable_pdfs_give_nothing(tmp_path, content):
    path = tmp_path / "bad.pdf"
    path.write_bytes(content)
    assert read_clues(path) is None


def test_image_only_pdf_gives_nothing(tmp_path):
    assert read_clues(make_pdf(tmp_path / "blank.pdf", [])) is None


def test_other_formats_are_not_read(tmp_path):
    path = tmp_path / "x.cbz"
    path.write_bytes(b"PK")
    assert read_clues(path) is None


def test_slow_pdf_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_text, "_read_text", lambda p, n: time.sleep(1) or "ISSN 0749-3851")
    path = make_pdf(tmp_path / "x.pdf", ["x"])
    started = time.monotonic()
    assert read_clues(path, timeout=0.1) is None
    assert time.monotonic() - started < 0.9


# Library scan


@pytest.fixture
def with_issns(session):  # noqa: F811
    mag(session, "2600: The Hacker Quarterly").issn = ISSN_2600
    mag(session, "Linux Magazine").issn = ISSN_LINUX
    session.flush()
    return session


def _items(s, scan):
    rows = s.scalars(select(ImportItem).where(ImportItem.scan_id == scan.id))
    return {r.source_path.replace("\\", "/").rsplit("/", 1)[-1]: r for r in rows}


def test_badly_named_pdf_is_placed_by_issn(with_issns, lib):  # noqa: F811
    s = with_issns
    make_pdf(lib / "scan0042.pdf", ["The Hacker Quarterly", "Autumn 2026", f"ISSN {ISSN_2600}"])
    item = _items(s, plan_scan(s, lib, ScanKind.LIBRARY))["scan0042.pdf"]
    assert item.action is ItemAction.MOVE
    assert (
        item.target_path
        == "2600 - The Hacker Quarterly/2026/2600 - The Hacker Quarterly - 2026-Autumn.pdf"
    )
    assert item.parsed["pdf_via"] == "ISSN" and "Autumn 2026" in item.parsed["pdf_summary"]


def test_issn_alone_only_suggests(with_issns, lib):  # noqa: F811
    s = with_issns
    make_pdf(lib / "scan0043.pdf", [f"ISSN {ISSN_2600}"])
    item = _items(s, plan_scan(s, lib, ScanKind.LIBRARY))["scan0043.pdf"]
    assert item.action is ItemAction.REVIEW
    assert item.magazine_id == mag(s, "2600: The Hacker Quarterly").id
    assert "suggested 2600" in item.reason


def test_title_in_pdf_needs_an_agreeing_clue(with_issns, lib):  # noqa: F811
    s = with_issns
    make_pdf(lib / "scan0044.pdf", ["FORTEAN TIMES", "October 2026", "Issue 475"])
    item = _items(s, plan_scan(s, lib, ScanKind.LIBRARY))["scan0044.pdf"]
    assert item.action is ItemAction.REVIEW and "suggested Fortean Times" in item.reason
    assert item.magazine_id == mag(s, "Fortean Times").id and item.issue_key == "2026-10"


def test_pdf_agreeing_with_the_name_moves(with_issns, lib):  # noqa: F811
    s = with_issns
    make_pdf(lib / "FT october 2026 x7.pdf", ["FORTEAN TIMES", "October 2026"])
    item = _items(s, plan_scan(s, lib, ScanKind.LIBRARY))["FT october 2026 x7.pdf"]
    assert item.action is ItemAction.MOVE, item.reason
    assert item.target_path.endswith("Fortean Times - 2026-10.pdf")
    assert any("agree on the month" in a for a in item.parsed["agreements"])


def test_name_and_pdf_disagree(with_issns, lib):  # noqa: F811
    s = with_issns
    make_pdf(lib / "2600 Summer 2026 extra 77.pdf", [f"ISSN {ISSN_2600}", "Autumn 2026"])
    item = _items(s, plan_scan(s, lib, ScanKind.LIBRARY))["2600 Summer 2026 extra 77.pdf"]
    assert item.action is ItemAction.REVIEW and "disagree" in item.reason


def test_pdf_issn_of_another_magazine_blocks(with_issns, lib):  # noqa: F811
    s = with_issns
    make_pdf(lib / "Linux Magazine 77.pdf", [f"ISSN {ISSN_2600}"])
    item = _items(s, plan_scan(s, lib, ScanKind.LIBRARY))["Linux Magazine 77.pdf"]
    assert item.action is ItemAction.REVIEW and "ISSN is 2600" in item.reason


def test_named_files_and_setting_off_skip_the_read(with_issns, lib, monkeypatch):  # noqa: F811
    s = with_issns
    calls = []
    monkeypatch.setattr(pdf_text, "read_clues", lambda p, n=2, timeout=5: calls.append(p))
    make_pdf(lib / "Fortean Times - 2026-10.pdf", ["x"])
    plan_scan(s, lib, ScanKind.LIBRARY)
    assert calls == []  # the name was enough
    make_pdf(lib / "scan0045.pdf", ["x"])
    library.set_setting(s, pdf_text.ENABLED_KEY, False)
    plan_scan(s, lib, ScanKind.LIBRARY)
    assert calls == []
    library.set_setting(s, pdf_text.ENABLED_KEY, True)
    plan_scan(s, lib, ScanKind.LIBRARY)
    assert len(calls) == 1


# Download import edition check


def test_import_refuses_another_editions_issn(env):  # noqa: F811
    e = env
    mag(e.session, "Linux Magazine").issn = ISSN_LINUX
    mag(e.session, "AMAZONAS Magazine").issn = ISSN_AMAZONAS_DE
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    job_dir = e.complete / "Linux Magazine 311"
    make_pdf(job_dir / "Linux311.pdf", [f"ISSN {ISSN_AMAZONAS_DE}"])
    e.fake_s.complete(g.job_id, "/data/complete/mags/Linux Magazine 311")
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.REVIEW and "belongs to AMAZONAS Magazine" in g.error


def test_import_notes_a_matching_issn(env):  # noqa: F811
    e = env
    mag(e.session, "Linux Magazine").issn = ISSN_LINUX
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    make_pdf(e.complete / "Linux Magazine 311" / "Linux311.pdf", [f"ISSN {ISSN_LINUX}"])
    e.fake_s.complete(g.job_id, "/data/complete/mags/Linux Magazine 311")
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED
    imported = e.session.scalars(
        select(History).where(History.event == HistoryEvent.IMPORTED)
    ).one()
    assert imported.data["pdf_issn"] == "matches"


def test_import_without_issn_never_reads(env, monkeypatch):  # noqa: F811
    e = env
    calls = []
    monkeypatch.setattr(pdf_text, "read_clues", lambda *a, **k: calls.append(a))
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, g, {"Linux311.pdf": b"magazine"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED and calls == []


# Release window


def test_on_sale_days_sets_the_early_window(session):  # noqa: F811
    s = session
    linux = mag(s, "Linux Magazine")
    mags = list(s.scalars(select(Magazine)))
    title = "Linux Magazine USA - Issue 312, November 2026"
    assert evaluate(s, title, mags, TODAY, size=50 << 20).accepted  # global 45 days
    linux.on_sale_days = 5
    ev = evaluate(s, title, mags, TODAY, size=50 << 20)
    assert not ev.accepted and any("more than 12 days away" in r for r in ev.reasons)


def test_on_sale_days_sets_when_wanted_starts(session):  # noqa: F811
    from periodicool.schedule import sync_expected_issues

    s = session
    linux = mag(s, "Linux Magazine")
    sync_expected_issues(s, linux, TODAY)
    upcoming = [i for i in linux.issues if i.cover_date and i.cover_date > TODAY]
    assert upcoming
    soon = min(upcoming, key=lambda i: i.cover_date)
    days = (soon.cover_date - TODAY).days
    linux.on_sale_days = days - 1
    assert soon not in wanted_issues(s, linux, TODAY)
    linux.on_sale_days = days
    assert soon in wanted_issues(s, linux, TODAY)
    assert TODAY + timedelta(days=days) == soon.cover_date


# Magazine form


def test_form_validates_and_saves_issn(client):
    bad = client.post(
        "/magazines/add",
        data={"title": "X", "frequency": "monthly", "numbering": "date", "issn": "1234-5678"},
    )
    assert bad.status_code == 422 and "Not a valid ISSN" in bad.text
    ok = client.post(
        "/magazines/add",
        data={
            "title": "X",
            "frequency": "monthly",
            "numbering": "date",
            "issn": "07493851",
            "on_sale_days": "10",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    page = client.get(ok.headers["location"]).text
    assert "0749-3851" in page and "About 10 days before the cover date" in page
