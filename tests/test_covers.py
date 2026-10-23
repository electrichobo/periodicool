"""Covers from the newest issue's PDF, cached in /config/covers."""

from sqlalchemy import select

from periodicool import covers
from periodicool.models import Issue, Magazine

from pdfgen import JPEG, make_cover_pdf, make_pdf
from test_web_magazines import mags  # noqa: F401


def test_cover_jpeg_from_a_pdf(tmp_path):
    pdf = make_cover_pdf(tmp_path / "issue.pdf")
    assert covers.cover_jpeg(pdf) == JPEG
    assert covers.cover_jpeg(make_pdf(tmp_path / "text.pdf", ["No pictures here"])) is None
    assert covers.cover_jpeg(make_cover_pdf(tmp_path / "logo.pdf", 120, 60)) is None  # too small
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 nonsense")
    assert covers.cover_jpeg(tmp_path / "broken.pdf") is None
    assert covers.cover_jpeg(tmp_path / "issue.epub") is None


def link(session, mag_id, key, rel, day):
    issue = session.scalars(
        select(Issue).where(Issue.magazine_id == mag_id, Issue.key == key)
    ).first()
    if issue is None:
        issue = Issue(magazine_id=mag_id, key=key, expected=False, monitored=True)
        session.add(issue)
    issue.file_path, issue.cover_date = rel, day
    session.flush()
    return issue


def test_cards_show_the_newest_issue_cover(client, settings, mags):  # noqa: F811
    from datetime import date

    lib = settings.library_dir
    mag_id = mags["Fortean Times"]
    make_cover_pdf(lib / "Fortean Times" / "2026" / "FT - 2026-09.pdf")
    make_pdf(lib / "Fortean Times" / "2026" / "FT - 2026-08.pdf", ["old"])
    with client.app.state.sessions() as s:
        link(s, mag_id, "2026-08", "Fortean Times/2026/FT - 2026-08.pdf", date(2026, 8, 1))
        newest = link(s, mag_id, "2026-09", "Fortean Times/2026/FT - 2026-09.pdf", date(2026, 9, 1))
        s.commit()
        newest_id = newest.id
    client.post("/magazines/refresh-all")
    cached = covers.covers_dir(settings.config_dir) / f"{mag_id}-{newest_id}.jpg"
    assert cached.read_bytes() == JPEG
    page = client.get("/").text
    assert f'src="/covers/{mag_id}"' in page
    resp = client.get(f"/covers/{mag_id}")
    assert resp.status_code == 200 and resp.content == JPEG
    # The library file is only read.
    assert (lib / "Fortean Times" / "2026" / "FT - 2026-09.pdf").is_file()


def test_no_cover_is_remembered(client, settings, mags, monkeypatch):  # noqa: F811
    from datetime import date

    lib = settings.library_dir
    mag_id = mags["People"]
    make_pdf(lib / "People" / "2026" / "People - 2026-10-16.pdf", ["text only"])
    with client.app.state.sessions() as s:
        issue = link(
            s, mag_id, "2026-10-16", "People/2026/People - 2026-10-16.pdf", date(2026, 10, 16)
        )
        s.commit()
        issue_id = issue.id
    client.post("/magazines/refresh-all")
    folder = covers.covers_dir(settings.config_dir)
    assert (folder / f"{mag_id}-{issue_id}.none").is_file()
    assert not list(folder.glob(f"{mag_id}-*.jpg"))
    calls = []
    monkeypatch.setattr(covers, "cover_jpeg", lambda path: calls.append(path))
    with client.app.state.sessions() as s:
        covers.refresh_covers(s, lib, settings.config_dir, mag_id)
    assert calls == []  # not read again
    assert client.get(f"/covers/{mag_id}").status_code == 404
    with client.app.state.sessions() as s:
        assert s.get(Magazine, mag_id).cover_path is None
