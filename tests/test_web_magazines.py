"""Magazine screens over HTTP: index, add, detail, pop-ups, mass editor. Fakes only."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import library, search
from periodicool.cli import magazine_from_dict
from periodicool.models import History, Issue, IssueStatus, Magazine
from rows import add_client, add_indexer

from fake_prowlarr import KEY, URL, FakeProwlarr, release_json
from fake_sab import SAB_KEY, SAB_URL, FakeSab

EXAMPLES = Path(__file__).parent.parent / "examples" / "magazines.json"
L311 = "Linux Magazine USA - Issue 311, October 2026"


@pytest.fixture
def mags(client):
    with client.app.state.sessions() as s:
        for entry in json.loads(EXAMPLES.read_text(encoding="utf-8")):
            s.add(magazine_from_dict(entry))
        s.commit()
        ids = {m.title: m.id for m in s.scalars(select(Magazine))}
    client.post("/magazines/refresh-all")
    return ids


def toast(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger"])["toast"]


def session(client):
    return client.app.state.sessions()


# Index


def test_empty_index(client):
    page = client.get("/")
    assert page.status_code == 200 and "No magazines yet" in page.text


def test_index_shows_every_view_and_totals(client, mags):
    page = client.get("/").text
    for title in ("Fortean Times", "2600: The Hacker Quarterly", "People"):
        assert page.count(title) >= 3  # cards, table and overview
    assert 'data-view-switch="table"' in page and 'id="mag-table"' in page
    assert "Size on disk" in page and 'aria-label="Library totals"' in page


def test_titles_are_escaped_everywhere(client):
    with session(client) as s:
        s.add(
            magazine_from_dict(
                {"title": "<script>x()</script>", "frequency": "monthly", "numbering": "date"}
            )
        )
        s.commit()
        mag_id = s.scalars(select(Magazine.id)).one()
    for path in ("/", f"/magazines/{mag_id}", "/magazines/editor", f"/magazines/{mag_id}/edit"):
        html = client.get(path).text
        assert "<script>x()" not in html and "&lt;script&gt;x()" in html, path


# Add, preview, edit


def test_add_validates_and_keeps_input(client):
    resp = client.post(
        "/magazines/add", data={"title": "", "frequency": "monthly", "numbering": "date"}
    )
    assert resp.status_code == 422 and "Enter a title" in resp.text
    resp = client.post(
        "/magazines/add",
        data={"title": "Numbered", "frequency": "monthly", "numbering": "issue", "grace_days": "x"},
    )
    assert resp.status_code == 422
    assert "Must be a whole number" in resp.text and "known issue number" in resp.text
    assert 'value="Numbered"' in resp.text


def test_add_creates_magazine_and_expected_issues(client):
    resp = client.post(
        "/magazines/add",
        data={
            "title": "Wired: UK",
            "frequency": "monthly",
            "numbering": "date",
            "monitored": "on",
            "monitor_mode": "new_only",
            "tags": "tech, uk",
            "aliases": "Wired UK",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/magazines/")
    with session(client) as s:
        mag = s.scalars(select(Magazine)).one()
        assert mag.tags == ["tech", "uk"] and mag.aliases == ["Wired UK"] and mag.monitored
        assert s.scalars(select(Issue).where(Issue.magazine_id == mag.id)).first()


def test_preview_shows_folder_and_issues(client):
    resp = client.post(
        "/magazines/preview",
        data={
            "title": "2600: The Hacker Quarterly",
            "frequency": "seasonal",
            "numbering": "season",
        },
    )
    assert "2600 - The Hacker Quarterly" in resp.text and "Expected issues" in resp.text
    irregular = client.post(
        "/magazines/preview",
        data={"title": "Make:", "frequency": "irregular", "numbering": "volume"},
    )
    assert "Irregular" in irregular.text


def test_edit_saves_and_reports_errors(client, mags):
    mag_id = mags["Fortean Times"]
    form = client.get(f"/magazines/{mag_id}/edit").text
    assert 'id="modal-title"' in form and 'value="Fortean Times"' in form
    bad = client.post(f"/magazines/{mag_id}/edit", data={"title": "", "frequency": "monthly"})
    assert "Enter a title" in bad.text
    ok = client.post(
        f"/magazines/{mag_id}/edit",
        data={
            "title": "Fortean Times",
            "frequency": "thirteen_per_year",
            "numbering": "issue",
            "anchor_date": "2023-11-01",
            "anchor_number": "437",
            "grace_days": "21",
            "monitor_mode": "new_only",
            "tags": "weird",
        },
    )
    assert ok.headers["HX-Refresh"] == "true" and toast(ok)["kind"] == "ok"
    with session(client) as s:
        mag = s.get(Magazine, mag_id)
        assert mag.grace_days == 21 and mag.tags == ["weird"] and mag.monitored is False


# Detail and toggles


def test_detail_page(client, mags):
    page = client.get(f"/magazines/{mags['Linux Magazine']}")
    assert page.status_code == 200
    assert "Issues per year" in page.text and 'class="fold"' in page.text
    assert client.get("/magazines/9999").status_code == 404


def test_monitor_toggles(client, mags):
    mag_id = mags["Linux Magazine"]
    assert "not monitored" in toast(client.post(f"/magazines/{mag_id}/monitor"))["text"]
    with session(client) as s:
        issue = s.scalars(select(Issue).where(Issue.magazine_id == mag_id)).first()
        year = issue.cover_date.year
    client.post(f"/issues/{issue.id}/monitor")
    with session(client) as s:
        assert s.get(Issue, issue.id).monitored is False
    client.post(f"/magazines/{mag_id}/years/{year}/monitor", data={"monitored": "on"})
    with session(client) as s:
        assert s.get(Issue, issue.id).monitored is True


def _linked_issue(client, settings, mag_id):
    rel = "Linux Magazine/2026/Linux Magazine - 2026-09.pdf"
    path = settings.library_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"magazine")
    with session(client) as s:
        issue = s.scalars(select(Issue).where(Issue.magazine_id == mag_id)).first()
        issue.file_path, issue.status = rel, IssueStatus.IMPORTED
        s.commit()
        return issue.id, path


def test_issue_details_and_unlink_keep_the_file(client, settings, mags):
    issue_id, path = _linked_issue(client, settings, mags["Linux Magazine"])
    details = client.get(f"/issues/{issue_id}").text
    assert "Unlink file" in details and 'role="tablist"' in details
    resp = client.post(f"/issues/{issue_id}/unlink")
    assert "still in the library" in toast(resp)["text"]
    assert path.read_bytes() == b"magazine"
    with session(client) as s:
        issue = s.get(Issue, issue_id)
        assert issue.file_path is None and issue.status is IssueStatus.WANTED


def test_delete_keeps_files(client, settings, mags):
    mag_id = mags["Linux Magazine"]
    _, path = _linked_issue(client, settings, mag_id)
    resp = client.post(f"/magazines/{mag_id}/delete")
    assert resp.headers["HX-Redirect"] == "/?saved=deleted"
    assert path.exists()
    with session(client) as s:
        assert s.get(Magazine, mag_id) is None
    assert "Files on disk were kept" in client.get("/?saved=deleted").text


# Covers


def test_cover_served_only_from_inside_the_library(client, settings, mags):
    mag_id = mags["Fortean Times"]
    cover = settings.library_dir / "Fortean Times" / "cover.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"\xff\xd8jpeg")
    outside = settings.library_dir.parent / "secret.jpg"
    outside.write_bytes(b"nope")
    with session(client) as s:
        s.get(Magazine, mag_id).cover_path = "Fortean Times/cover.jpg"
        s.commit()
    assert client.get(f"/covers/{mag_id}").content == b"\xff\xd8jpeg"
    with session(client) as s:
        s.get(Magazine, mag_id).cover_path = "../secret.jpg"
        s.commit()
    assert client.get(f"/covers/{mag_id}").status_code == 404


# Scan and rename


def test_scan_preview_then_apply(client, settings, mags):
    folder = settings.library_dir / "Fortean Times"
    folder.mkdir(parents=True)
    (folder / "Fortean.Times.Issue.351.TruePDF-March.2017.pdf").write_bytes(b"ft")
    preview = client.get(f"/magazines/{mags['Fortean Times']}/scan").text
    assert "Preview only" in preview and "Fortean Times/2017/Fortean Times - 2017-03.pdf" in preview
    assert (folder / "Fortean.Times.Issue.351.TruePDF-March.2017.pdf").exists()
    scan_id = int(preview.split("/scans/")[1].split("/")[0])
    resp = client.post(f"/scans/{scan_id}/apply")
    assert "moved 1" in toast(resp)["text"]
    assert (folder / "2017" / "Fortean Times - 2017-03.pdf").read_bytes() == b"ft"


def test_scan_without_folder(client, mags):
    assert "Folder not found" in client.get(f"/magazines/{mags['People']}/scan").text


# Searching and grabbing


def test_search_without_prowlarr(client, mags):
    page = client.get(f"/magazines/{mags['Linux Magazine']}/interactive/results").text
    assert "No indexer is set up yet" in page
    assert toast(client.post(f"/magazines/{mags['Linux Magazine']}/search"))["kind"] == "warn"
    assert toast(client.post("/tasks/rss"))["kind"] == "warn"


@pytest.fixture
def fakes(client, monkeypatch):
    monkeypatch.setenv(search.API_KEY_ENV, KEY)
    monkeypatch.setenv("SABNZBD_API_KEY", SAB_KEY)
    fake_p, fake_s = FakeProwlarr(), FakeSab()
    client.app.state.prowlarr_transport = fake_p
    client.app.state.sab_transport = fake_s
    with session(client) as s:
        add_indexer(s, URL)
        library.set_setting(s, search.MIN_INTERVAL_KEY, 0)
        add_client(s, SAB_URL)
        s.commit()
    return fake_p, fake_s


def test_interactive_search_lists_reasons_and_grabs(client, mags, fakes):
    fake_p, fake_s = fakes
    fake_p.search_results = [
        release_json(L311),
        release_json("People.Australia.TruePDF-08.April.2019", guid="g2"),
    ]
    page = client.get(f"/magazines/{mags['Linux Magazine']}/interactive/results").text
    assert L311 in page and "People.Australia" in page
    assert "Accept" in page and "Reject" in page and "Grab" in page
    with session(client) as s:
        from periodicool.models import Release

        rel_id = s.scalars(select(Release.id).where(Release.title == L311)).one()
    resp = client.post(f"/releases/{rel_id}/grab")
    assert toast(resp)["kind"] == "ok" and fake_s.added[0][0] == "mags"
    with session(client) as s:
        assert s.scalars(select(History)).one().source_title == L311
    # Filtered to one issue: the other release is left out.
    page = client.get(f"/magazines/{mags['Linux Magazine']}/interactive/results?issue=n311").text
    assert L311 in page and "People.Australia" not in page


def test_automatic_search_grabs_wanted_issue(client, mags, fakes):
    fake_p, fake_s = fakes
    fake_p.search_results = [release_json(L311)]
    resp = client.post(f"/magazines/{mags['Linux Magazine']}/search", data={"issue": "n311"})
    assert "grabbed n311" in toast(resp)["text"], toast(resp)
    assert len(fake_s.added) == 1


# Mass editor


def test_mass_editor_changes_and_deletes(client, settings, mags):
    page = client.get("/magazines/editor").text
    assert 'class="row-select"' in page and "Format profile" in page
    ids = [mags["Fortean Times"], mags["People"]]
    resp = client.post(
        "/magazines/editor",
        data={
            "ids": [str(i) for i in ids],
            "monitored": "no",
            "add_tags": "weird, print",
            "action": "save",
        },
    )
    assert resp.headers["HX-Redirect"] == "/magazines/editor?saved=editor"
    with session(client) as s:
        for i in ids:
            mag = s.get(Magazine, i)
            assert mag.monitored is False and mag.tags == ["weird", "print"]
    client.post("/magazines/editor", data={"ids": [str(ids[0])], "remove_tags": "WEIRD"})
    with session(client) as s:
        assert s.get(Magazine, ids[0]).tags == ["print"]
    assert toast(client.post("/magazines/editor", data={"action": "save"}))["kind"] == "warn"
    client.post("/magazines/editor", data={"ids": [str(ids[1])], "action": "delete"})
    with session(client) as s:
        assert s.get(Magazine, ids[1]) is None and s.get(Magazine, ids[0]) is not None


def test_interactive_search_opens_at_once_then_loads_live(client, mags):
    mag_id = mags["Linux Magazine"]
    shell = client.get(f"/magazines/{mag_id}/interactive?issue=n311").text
    assert "Asking the indexers" in shell
    assert f'hx-get="/magazines/{mag_id}/interactive/results?issue=n311"' in shell
    assert 'hx-trigger="load"' in shell


def test_readout_lists_running_and_finished_tasks(client):
    from periodicool import activity_feed

    activity_feed.reset()
    assert client.get("/readout").json() == {"running": [], "recent": []}
    with activity_feed.task("Search: Wired"):
        activity_feed.say("NZBgeek: searching")
        now = client.get("/readout").json()
        assert now["running"][0]["title"] == "Search: Wired"
        assert now["running"][0]["line"] == "NZBgeek: searching"
    done = client.get("/readout").json()
    assert done["running"] == [] and done["recent"][0]["ok"] is True
    try:
        with activity_feed.task("RSS sync"):
            raise RuntimeError("indexer down")
    except RuntimeError:
        pass
    assert client.get("/readout").json()["recent"][-1]["line"] == "Failed: indexer down"
    activity_feed.reset()


def test_search_buttons_have_no_confirm(client, mags):
    page = client.get("/").text
    button = page[page.index('hx-post="/magazines/search-missing"') :][:300]
    assert "hx-confirm" not in button and "data-activity" in button
    detail = client.get(f"/magazines/{mags['Linux Magazine']}").text
    button = detail[detail.index("Search monitored") - 300 :][:300]
    assert "hx-confirm" not in button
