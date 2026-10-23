"""Opt-in metadata lookups: off by default, cached, applied only when ticked."""

import json

import pytest
from sqlalchemy import select

from periodicool import library
from periodicool.decisions import evaluate
from periodicool.lookups import archive, loc, wikidata
from periodicool.lookups.http import USER_AGENT
from periodicool.models import Issue, LookupCache, Magazine, MonitorMode

from fake_lookups import FakeLookups, load
from test_web_magazines import mags, session, toast  # noqa: F401

ISSN = "0749-3851"
T2600 = "2600: The Hacker Quarterly"


@pytest.fixture
def fake(client):
    f = FakeLookups()
    client.app.state.lookup_transport = f
    return f


def turn_on(client, *sources):
    with session(client) as s:
        for src in sources:
            library.set_setting(s, f"lookups.{src}", True)
        s.commit()


def set_issn(client, mag_id, issn=ISSN):
    with session(client) as s:
        s.get(Magazine, mag_id).issn = issn
        s.commit()


# Parsing the recorded answers


def test_parse_library_of_congress():
    rec = loc.lookup(issn=ISSN, transport=FakeLookups())
    assert rec.title == "2600" and "Twenty-six hundred" in rec.alt_titles
    assert rec.issns == [ISSN] and rec.frequency == "quarterly" and rec.start_year == 1984
    assert "Quarterly" in rec.frequency_text and "Monthly" in rec.former_frequency_text


def test_parse_wikidata_by_issn_and_title():
    by_issn = wikidata.lookup(issn=ISSN, transport=FakeLookups())
    assert by_issn.title == T2600 and "2600 Magazine" in by_issn.alt_titles
    assert by_issn.start_year == 1984 and by_issn.ref == "Q219293"
    by_title = wikidata.lookup(title=T2600, transport=FakeLookups())
    assert by_title.ref == "Q219293"


def test_back_issues_from_item_titles():
    from dev_magazines import BY_FIXTURE

    items = archive.search(T2600, transport=FakeLookups())
    found, skipped = archive.back_issues(BY_FIXTURE["2600"], items)
    keys = {b.key for b in found}
    assert "2019-autumn" in keys and "1998-spring" in keys
    autumn = next(b for b in found if b.key == "2019-autumn")
    assert (autumn.volume_number, autumn.issue_number) == (36, 3)
    assert skipped > 0  # titles without a season or year are left out


def test_frequency_guess():
    assert loc.frequency_guess("Monthly (except Aug.)") == "monthly"
    assert loc.frequency_guess("Bimonthly") == "bimonthly"
    assert loc.frequency_guess("Irregular") is None


# Off by default: nothing leaves the machine


def test_nothing_is_sent_while_off(client, mags, fake, settings):
    mag_id = mags[T2600]
    set_issn(client, mag_id)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "scan.pdf").write_bytes(b"x")
    client.get("/")
    client.get(f"/magazines/{mag_id}")
    popup = client.get(f"/magazines/{mag_id}/metadata").text
    assert "Metadata lookups are off" in popup and "Settings, Metadata" in popup
    client.post("/magazines/add/search", data={"term": T2600})
    client.post("/library/scan")
    client.post("/system/tasks/metadata/run")
    client.get("/system/health")
    assert fake.calls == []


def test_banner_and_dismiss(client, mags):
    assert 'id="lookup-notice"' in client.get("/").text
    client.post("/lookups/dismiss")
    assert 'id="lookup-notice"' not in client.get("/").text
    with session(client) as s:
        library.set_setting(s, "lookups.notice_dismissed", False)
        library.set_setting(s, "lookups.wikidata", True)
        s.commit()
    assert 'id="lookup-notice"' not in client.get("/").text  # a source is on


def test_setup_page_mentions_lookups(anon_client):
    assert "metadata lookups" in anon_client.get("/setup").text


def test_settings_explain_lookups(client):
    page = client.get("/settings/metadata").text
    assert "Off by default" in page and "Only an ISSN" in page
    assert page.count('type="checkbox" role="switch"') >= 3


# On: what is sent, caching


def test_popup_lookup_sends_only_the_issn_and_caches(client, mags, fake):
    mag_id = mags[T2600]
    set_issn(client, mag_id)
    turn_on(client, "loc", "wikidata")
    page = client.get(f"/magazines/{mag_id}/metadata").text
    assert "Twenty-six hundred" in page and "Quarterly" in page and "Apply ticked" in page
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call["headers"]["User-Agent"] == USER_AGENT
        assert ISSN in call["url"] and "Hacker" not in call["url"]
    client.get(f"/magazines/{mag_id}/metadata")
    assert len(fake.calls) == 2  # cached
    client.get(f"/magazines/{mag_id}/metadata?refresh=true")
    assert len(fake.calls) == 4


def test_failures_are_cached_for_a_day(client, mags):
    failing = FakeLookups({})
    client.app.state.lookup_transport = failing
    mag_id = mags[T2600]
    set_issn(client, mag_id)
    turn_on(client, "loc")
    page = client.get(f"/magazines/{mag_id}/metadata").text
    assert "answered 404" in page
    client.get(f"/magazines/{mag_id}/metadata")
    assert len(failing.calls) == 1
    with session(client) as s:
        assert s.scalars(select(LookupCache)).one().error


def test_new_issn_triggers_one_lookup(client, mags, fake):
    turn_on(client, "loc")
    mag_id = mags[T2600]
    form = client.get(f"/magazines/{mag_id}/edit")
    assert form.status_code == 200
    data = {
        "title": T2600,
        "frequency": "seasonal",
        "numbering": "season",
        "monitor_mode": "new_only",
        "grace_days": "14",
        "issn": ISSN,
        "monitored": "on",
    }
    client.post(f"/magazines/{mag_id}/edit", data=data)
    assert len(fake.calls) == 1
    client.post(f"/magazines/{mag_id}/edit", data=data)  # same ISSN: no new lookup
    assert len(fake.calls) == 1


# Applying


def test_apply_only_what_is_ticked(client, mags, fake):
    mag_id = mags[T2600]
    set_issn(client, mag_id)
    turn_on(client, "loc", "wikidata")
    client.get(f"/magazines/{mag_id}/metadata")
    resp = client.post(
        f"/magazines/{mag_id}/metadata/apply",
        data={"apply": ["alias:Twenty-six hundred", "alias:2600 Magazine", "alias:2600 magazine"]},
    )
    assert "alias Twenty-six hundred" in toast(resp)["text"]
    with session(client) as s:
        mag = s.get(Magazine, mag_id)
        assert mag.aliases == ["Twenty-six hundred", "2600 Magazine"]  # no case duplicate
        assert mag.frequency.value == "seasonal"  # not ticked, not changed
    client.post(f"/magazines/{mag_id}/metadata/apply", data={"apply": ["frequency:quarterly"]})
    with session(client) as s:
        assert s.get(Magazine, mag_id).frequency.value == "quarterly"
    assert toast(client.post(f"/magazines/{mag_id}/metadata/apply"))["kind"] == "warn"


def test_add_search_cards_from_catalogs(client, fake):
    page = client.get("/magazines/add").text
    assert "nothing to search" in page and 'name="term"' in page
    turn_on(client, "wikidata", "loc")
    results = client.post("/magazines/add/search", data={"term": T2600}).text
    assert "Wikidata" in results and ISSN in results and "/magazines/add/pick" in results
    calls = len(fake.calls)
    client.post("/magazines/add/search", data={"term": T2600})
    assert len(fake.calls) == calls  # the second search comes from the cache


def test_back_issues_list_and_add(client, mags, fake):
    mag_id = mags[T2600]
    turn_on(client, "archive")
    page = client.get(f"/magazines/{mag_id}/metadata").text
    assert "monitored for all issues or from a year on" in page  # new-only by default
    with session(client) as s:
        mag = s.get(Magazine, mag_id)
        mag.monitor_mode, mag.monitor_from_year = MonitorMode.FROM_YEAR, 1998
        s.commit()
    page = client.get(f"/magazines/{mag_id}/metadata").text
    assert "Volume 36 Issue 3 - Autumn 2019" in page and "Autumn 1995" not in page
    resp = client.post(
        f"/magazines/{mag_id}/metadata/back-issues", data={"keys": ["2019-autumn", "1998-spring"]}
    )
    assert "Added 2" in toast(resp)["text"]
    with session(client) as s:
        keys = set(s.scalars(select(Issue.key).where(Issue.magazine_id == mag_id)))
        assert {"2019-autumn", "1998-spring"} <= keys
    assert "Volume 36 Issue 3 - Autumn 2019" not in client.get(f"/magazines/{mag_id}/metadata").text


def test_anchor_from_a_numbered_back_issue(client, mags):
    items = [{"identifier": "ft-449", "title": "Fortean Times No 449 October 2024", "date": "2024"}]
    responses = load() | {
        'archive title:("Fortean Times") AND mediatype:texts': json.dumps(
            {"response": {"docs": items}}
        )
    }
    client.app.state.lookup_transport = FakeLookups(responses)
    turn_on(client, "archive")
    mag_id = mags["Fortean Times"]
    with session(client) as s:
        mag = s.get(Magazine, mag_id)
        mag.monitor_mode = MonitorMode.ALL
        s.commit()
    page = client.get(f"/magazines/{mag_id}/metadata").text
    assert 'name="anchor" value="2024-10"' in page
    client.post(f"/magazines/{mag_id}/metadata/back-issues", data={"anchor": "2024-10"})
    with session(client) as s:
        mag = s.get(Magazine, mag_id)
        assert mag.anchor_number == 449 and str(mag.anchor_date) == "2024-10-01"


# Behind the scenes


def test_alternate_titles_help_library_files_only(client, mags, fake, settings):
    mag_id = mags[T2600]
    set_issn(client, mag_id)
    turn_on(client, "loc")
    client.get(f"/magazines/{mag_id}/metadata")  # fills the cache
    lib = settings.library_dir
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "Twenty-six hundred - Autumn 2026.pdf").write_bytes(b"x")
    calls = len(fake.calls)
    target = client.post("/library/scan").headers["HX-Redirect"]
    preview = client.get(target).text
    assert (
        "2600 - The Hacker Quarterly/2026/2600 - The Hacker Quarterly - 2026-Autumn.pdf" in preview
    )
    assert len(fake.calls) == calls  # the scan used the cache, no new requests
    with session(client) as s:
        from periodicool.models import ImportItem

        item = s.scalars(select(ImportItem)).one()
        assert any("Library of Congress alternate title" in n for n in item.parsed["notes"])
        mags_all = list(s.scalars(select(Magazine)))
        from datetime import date

        ev = evaluate(
            s, "Twenty-six hundred - Autumn 2026", mags_all, date(2026, 10, 1), size=50 << 20
        )
        assert not ev.accepted  # releases never use these names


def test_health_frequency_mismatch(client, mags, fake):
    mag_id = mags[T2600]
    set_issn(client, mag_id)
    turn_on(client, "loc")
    client.get(f"/magazines/{mag_id}/metadata")
    assert "Frequency of" not in client.get("/system/health").text  # seasonal = quarterly
    with session(client) as s:
        s.get(Magazine, mag_id).frequency = "monthly"
        s.commit()
    page = client.get("/system/health").text
    assert "Frequency of 2600" in page and "Library of Congress says Quarterly" in page


def test_metadata_job(client, mags, fake):
    resp = client.post("/system/tasks/metadata/run")
    assert "lookups are off" in toast(resp)["text"] and fake.calls == []
    set_issn(client, mags[T2600])
    turn_on(client, "loc", "wikidata")
    resp = client.post("/system/tasks/metadata/run")
    assert "1 magazines checked, 2 lookups" in toast(resp)["text"]
    resp = client.post("/system/tasks/metadata/run")
    assert "0 lookups" in toast(resp)["text"]  # cached


def test_settings_test_buttons(client, fake):
    assert toast(client.post("/settings/metadata/test/loc"))["kind"] == "warn"  # off
    turn_on(client, "loc", "archive")
    assert "answered: 2600" in toast(client.post("/settings/metadata/test/loc"))["text"]
    assert "13 items" in toast(client.post("/settings/metadata/test/archive"))["text"]
    assert client.post("/settings/metadata/test/bogus").status_code == 404


# Monitor pop-up


def test_monitor_popup_adds_back_issues(client, mags):
    mag_id = mags["American Cinematographer"]
    page = client.get(f"/magazines/{mag_id}").text
    assert f'hx-get="/magazines/{mag_id}/monitoring"' in page
    popup = client.get(f"/magazines/{mag_id}/monitoring").text
    assert "New issues only" in popup and "From a year" in popup and "All issues" in popup
    with session(client) as s:
        before = s.scalars(select(Issue).where(Issue.magazine_id == mag_id)).all()
        assert all(i.year is None or i.year >= 2026 for i in before)
    bad = client.post(
        f"/magazines/{mag_id}/monitoring", data={"monitored": "on", "monitor_mode": "from_year"}
    )
    assert "Enter a year" in bad.text
    resp = client.post(
        f"/magazines/{mag_id}/monitoring",
        data={
            "monitored": "on",
            "monitor_mode": "from_year",
            "monitor_from_year": "2022",
            "monitor_existing": "on",
        },
    )
    text = toast(resp)["text"]
    assert resp.headers["HX-Refresh"] == "true" and "expected issues added" in text
    added = int(text.split()[0])
    assert added >= 40  # 2022 through 2025 at least
    with session(client) as s:
        mag = s.get(Magazine, mag_id)
        assert mag.monitor_mode is MonitorMode.FROM_YEAR and mag.monitor_from_year == 2022
        years = set(s.scalars(select(Issue.year).where(Issue.magazine_id == mag_id)))
        assert {2022, 2023, 2024, 2025} <= years
    again = client.post(
        f"/magazines/{mag_id}/monitoring",
        data={"monitored": "on", "monitor_mode": "new_only"},
    )
    assert toast(again)["text"].startswith("0 expected issues added")


def test_monitor_popup_offers_archive_issues_when_on(client, mags, fake):
    mag_id = mags[T2600]
    turn_on(client, "archive")
    resp = client.post(
        f"/magazines/{mag_id}/monitoring",
        data={"monitored": "on", "monitor_mode": "from_year", "monitor_from_year": "1998"},
    )
    assert "HX-Refresh" not in resp.headers
    assert "Past issues from the Internet Archive" in resp.text
    assert "expected issues added" in resp.text
    # The schedule already lists every season from 1998, so nothing new is offered.
    assert "No new past issues" in resp.text
