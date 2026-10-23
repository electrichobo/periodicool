"""Settings tabs over HTTP. Secrets never appear; invalid input saves nothing."""

import pytest
from sqlalchemy import select

from periodicool import auth, library, search
from periodicool.models import DownloadClient, FormatProfile, Indexer, Job, Magazine
from rows import add_client, add_indexer

from fake_prowlarr import URL
from fake_sab import SAB_URL
from test_web_magazines import fakes, mags, session, toast  # noqa: F401


def setting(client, key, default=None):
    with session(client) as s:
        return library.get_setting(s, key, default)


@pytest.mark.parametrize(
    "tab", ["media", "profiles", "indexers", "downloads", "connect", "tags", "general", "ui"]
)
def test_every_tab_renders(client, tab):
    page = client.get(f"/settings/{tab}")
    assert page.status_code == 200 and 'aria-label="Settings"' in page.text
    assert client.get("/settings/bogus").status_code == 404


def test_settings_redirect(client):
    assert client.get("/settings", follow_redirects=False).headers["location"] == "/settings/media"


def test_secrets_never_shown(client, monkeypatch):
    monkeypatch.setenv(search.API_KEY_ENV, "prowlarr-secret-value")
    monkeypatch.setenv("SABNZBD_API_KEY", "sab-secret-value")
    with session(client) as s:
        idx = add_indexer(s, URL)
        nzb = add_indexer(s, "https://nzb.example", "stored-newznab-key", "Direct", "newznab")
        sab = add_client(s, SAB_URL, "stored-sab-key")
        s.commit()
        ids = idx.id, nzb.id, sab.id
    pages = [client.get(f"/settings/{tab}").text for tab in ("indexers", "downloads", "general")]
    pages += [
        client.get(f"/settings/indexers/{ids[0]}/edit").text,
        client.get(f"/settings/indexers/{ids[1]}/edit").text,
        client.get(f"/settings/downloads/{ids[2]}/edit").text,
    ]
    secrets = ("prowlarr-secret-value", "sab-secret-value", "stored-newznab-key", "stored-sab-key")
    for text in pages:
        for secret in secrets:
            assert secret not in text
    assert "From the environment variable" in pages[3] and "Key set" in pages[4]


def test_save_indexers_converts_units_and_updates_jobs(client):
    with session(client) as s:
        s.add(Job(name="rss", enabled=True, interval_s=900))
        s.commit()
    form = {
        "search__categories": "7010, 7020",
        "jobs__rss__interval_s": "30",
        "jobs__wanted__interval_s": "12",
        "search__cache_hours": "6",
        "search__min_interval_s": "2.5",
        "search__daily_cap": "",
        "search__early_days": "45",
        "release__min_size_mb": "1",
        "release__must_contain": "",
        "release__must_not_contain": "German, French",
    }
    resp = client.post("/settings/indexers", data=form, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/settings/indexers?saved=1"
    assert setting(client, "search.categories") == [7010, 7020]
    assert setting(client, "jobs.rss.interval_s") == 1800
    assert setting(client, "jobs.wanted.interval_s") == 12 * 3600
    assert setting(client, "search.daily_cap") is None
    assert setting(client, "release.must_not_contain") == ["German", "French"]
    with session(client) as s:
        assert s.get(Job, "rss").interval_s == 1800
    page = client.get("/settings/indexers?saved=1").text
    assert 'value="30"' in page and "Saved." in page


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("search__categories", "magazines", "Numbers separated"),
        ("search__cache_hours", "500", "At most 168"),
        ("jobs__rss__interval_s", "1", "At least 5"),
        ("search__early_days", "soon", "whole number"),
    ],
)
def test_bad_input_saves_nothing(client, field, value, message):
    form = {
        "search__categories": "7010",
        "jobs__rss__interval_s": "15",
        "jobs__wanted__interval_s": "6",
        "search__cache_hours": "6",
        "search__min_interval_s": "2",
        "search__early_days": "45",
        "release__min_size_mb": "1",
        field: value,
    }
    resp = client.post("/settings/indexers", data=form)
    assert resp.status_code == 422 and message in resp.text and "Nothing was saved" in resp.text
    assert f'value="{value}"' in resp.text or value in resp.text
    assert setting(client, "search.categories") is None


def test_media_template_validation_and_preview(client):
    preview = client.post(
        "/settings/media/preview", data={"naming__template": "{Title}/{Date}.{ext}"}
    )
    assert (
        "Fortean Times/2026-10.pdf" in preview.text
        and "2600 - The Hacker Quarterly" in preview.text
    )
    bad = client.post("/settings/media/preview", data={"naming__template": "{Title}/{Nope}.{ext}"})
    assert "Unknown token {Nope}" in bad.text
    for template, message in [
        ("{Title}/{Date}", "End the template"),
        ("{Title}/{Year}.{ext}", "Include {Date}"),
    ]:
        resp = client.post("/settings/media", data={"naming__template": template})
        assert resp.status_code == 422 and message in resp.text
    ok = client.post(
        "/settings/media",
        data={
            "naming__template": "{Title}/{Year}/{Title} {Date}.{ext}",
            "import__auto_apply": "on",
            "pdf__pages": "3",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert setting(client, "naming.template") == "{Title}/{Year}/{Title} {Date}.{ext}"
    assert setting(client, "downloads.remove_leftovers") is False  # switch left off
    assert setting(client, "import.auto_apply") is True
    assert setting(client, "pdf.pages") == 3 and setting(client, "pdf.read_text") is False


def test_media_tab_shows_folders(client, settings):
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    page = client.get("/settings/media").text
    assert str(settings.library_dir) in page and "Not set" in page  # no import folder


def test_downloads_settings_without_client_fields(client):
    form = {"grab__max_per_run": "10", "jobs__downloads__interval_s": "60"}
    resp = client.post("/settings/downloads", data=form, follow_redirects=False)
    assert resp.status_code == 303 and setting(client, "grab.auto") is False


# Indexer and download client lists


def test_add_indexer_kind_picker_then_form(client):
    picker = client.get("/settings/indexers/new").text
    assert "Prowlarr" in picker and "Newznab" in picker and "?kind=newznab" in picker
    form = client.get("/settings/indexers/new?kind=newznab").text
    assert 'name="api_key"' in form and "API calls per day" in form
    assert 'type="password"' in form


def test_add_edit_delete_indexer_with_write_only_key(client):
    base = {
        "kind": "newznab",
        "name": "Direct <b>",
        "url": "https://nzb.example/api/",
        "priority": "10",
        "enabled": "on",
        "categories": "7010",
        "daily_cap": "100",
    }
    missing = client.post("/settings/indexers/save", data=base)
    assert missing.status_code == 200 and "Enter the API key." in missing.text
    assert "Direct &lt;b&gt;" in missing.text and "Direct <b>" not in missing.text
    resp = client.post("/settings/indexers/save", data=base | {"api_key": "sekrit-key-123"})
    assert resp.headers["HX-Redirect"] == "/settings/indexers?saved=1"
    with session(client) as s:
        row = s.scalars(select(Indexer)).one()
        assert row.url == "https://nzb.example/api" and row.api_key == "sekrit-key-123"
        assert row.settings == {"daily_cap": 100} and row.priority == 10
        row_id = row.id
    page = client.get("/settings/indexers").text
    assert "Direct &lt;b&gt;" in page and "sekrit-key-123" not in page
    # An empty key field keeps the saved key.
    edit = base | {"id": str(row_id), "name": "Direct", "enabled": ""}
    client.post("/settings/indexers/save", data=edit)
    with session(client) as s:
        row = s.get(Indexer, row_id)
        assert row.api_key == "sekrit-key-123" and row.name == "Direct" and not row.enabled
    assert "sekrit-key-123" in client.app.state.logs.redactor.secrets
    dup = client.post("/settings/indexers/save", data=base | {"name": "direct", "api_key": "x"})
    assert "already has this name" in dup.text
    bad = client.post(
        "/settings/indexers/save", data=base | {"url": "http://u:p@h", "api_key": "k"}
    )
    assert "Leave keys" in bad.text
    client.post(f"/settings/indexers/{row_id}/delete")
    with session(client) as s:
        assert s.scalars(select(Indexer)).all() == []


def test_first_prowlarr_may_use_the_environment_key(client, monkeypatch):
    monkeypatch.setenv(search.API_KEY_ENV, "env-key-value")
    data = {"kind": "prowlarr", "name": "Prowlarr", "url": "http://prowlarr:9696", "priority": "25"}
    resp = client.post("/settings/indexers/save", data=data)
    assert resp.status_code == 204
    second = client.post("/settings/indexers/save", data=data | {"name": "Second"})
    assert "Enter the API key." in second.text


def test_download_client_category_and_kept_mappings(client):
    base = {
        "kind": "sabnzbd",
        "name": "SABnzbd",
        "url": "http://sab:8080",
        "api_key": "sab-key-1",
        "priority": "1",
        "enabled": "on",
        "category": "mags",
    }
    no_cat = client.post("/settings/downloads/save", data=base | {"category": ""})
    assert "its own category" in no_cat.text
    assert client.post("/settings/downloads/save", data=base).status_code == 204
    with session(client) as s:
        row = s.scalars(select(DownloadClient)).one()
        assert row.path_map == [] and row.category == "mags"
        row.path_map = [["/data", "/downloads"]]
        s.commit()
        row_id = row.id
    # Editing the client keeps its mappings; they have their own list now.
    edit = client.post("/settings/downloads/save", data=base | {"id": str(row_id), "api_key": ""})
    assert edit.status_code == 204
    with session(client) as s:
        assert s.get(DownloadClient, row_id).path_map == [["/data", "/downloads"]]
    page = client.get("/settings/downloads").text
    assert "SABnzbd" in page and "sab-key-1" not in page and "<code>mags</code>" in page
    assert 'id="mappings"' in page and "<code>/data</code>" in page


def test_remote_path_mappings_across_two_clients(client):
    with session(client) as s:
        one = add_client(s, "http://sab:8080", "k1-secret", "First")
        two = add_client(s, "http://sab2:8080", "k2-secret", "Second", category="mags2")
        s.commit()
        one_id, two_id = one.id, two.id
    form = client.get("/settings/downloads/mappings/new").text
    assert 'data-browse="src-local"' in form and "First" in form and "Second" in form
    half = client.post(
        "/settings/downloads/mappings/save", data={"client_id": str(one_id), "remote": "/data"}
    )
    assert "Enter the same folder as PeriodiCool sees it." in half.text
    for cid, remote, local in [
        (one_id, "/data/complete/mags/", "/downloads/"),
        (two_id, "/mnt/sab2", "/downloads2"),
        (one_id, "/other", "/x"),
    ]:
        resp = client.post(
            "/settings/downloads/mappings/save",
            data={"client_id": str(cid), "remote": remote, "local": local},
        )
        assert resp.status_code == 204
    with session(client) as s:
        assert s.get(DownloadClient, one_id).path_map == [
            ["/data/complete/mags", "/downloads"],
            ["/other", "/x"],
        ]
        assert s.get(DownloadClient, two_id).path_map == [["/mnt/sab2", "/downloads2"]]
    page = client.get("/settings/downloads").text
    assert page.count("/settings/downloads/mappings/") >= 6  # edit and delete per mapping
    edit = client.get(f"/settings/downloads/mappings/{one_id}/1").text
    assert 'value="/other"' in edit and 'name="old_index" value="1"' in edit
    # Moving a mapping to the other client takes it off the first.
    moved = {
        "client_id": str(two_id),
        "remote": "/other",
        "local": "/y",
        "old_client": str(one_id),
        "old_index": "1",
    }
    assert client.post("/settings/downloads/mappings/save", data=moved).status_code == 204
    with session(client) as s:
        assert s.get(DownloadClient, one_id).path_map == [["/data/complete/mags", "/downloads"]]
        assert s.get(DownloadClient, two_id).path_map == [
            ["/mnt/sab2", "/downloads2"],
            ["/other", "/y"],
        ]
    client.post(f"/settings/downloads/mappings/{two_id}/0/delete")
    with session(client) as s:
        assert s.get(DownloadClient, two_id).path_map == [["/other", "/y"]]
    assert client.post(f"/settings/downloads/mappings/{two_id}/5/delete").status_code == 404


def test_folder_browser_lists_folders_only(client, tmp_path):
    (tmp_path / "Comics").mkdir()
    (tmp_path / "Mags").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    page = client.get(
        "/filesystem", params={"path": tmp_path.as_posix(), "target": "src-local"}
    ).text
    assert "Comics/" in page and "Mags/" in page
    assert "notes.txt" not in page and ".hidden" not in page
    assert f'data-pick-folder="{tmp_path.as_posix()}" data-target="src-local"' in page
    assert "Up one level" in page
    # A missing folder shows its nearest existing parent; a bad target is dropped.
    missing = client.get(
        "/filesystem", params={"path": (tmp_path / "nope" / "deeper").as_posix(), "target": 'x"y'}
    ).text
    assert "Comics/" in missing and "data-pick-folder" not in missing


def test_folder_browser_needs_login(settings):
    from fastapi.testclient import TestClient

    from periodicool.main import create_app

    with TestClient(create_app(settings)) as anon:
        resp = anon.get("/filesystem", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)


def test_connection_tests_without_setup(client):
    resp = client.post("/settings/indexers/test", data={"kind": "prowlarr"})
    assert toast(resp)["kind"] == "warn"
    assert toast(client.post("/settings/downloads/test", data={}))["kind"] == "warn"


def test_connection_tests_with_fakes(client, fakes):
    with session(client) as s:
        idx = s.scalars(select(Indexer)).one().id
        sab = s.scalars(select(DownloadClient)).one().id
    page = client.post(f"/settings/indexers/{idx}/test").text
    assert "Connected" in page and "Prowlarr:" in page
    assert "answered" in toast(client.post(f"/settings/downloads/{sab}/test"))["text"]
    form = {"id": str(idx), "kind": "prowlarr", "name": "Prowlarr", "url": URL}
    assert "Prowlarr answered" in toast(client.post("/settings/indexers/test", data=form))["text"]
    form = {"id": str(sab), "kind": "sabnzbd", "url": SAB_URL, "category": "mags"}
    assert "answered" in toast(client.post("/settings/downloads/test", data=form))["text"]


def test_api_key_shown_and_regenerated(client, settings):
    old = auth.api_key(settings.config_dir)
    assert old in client.get("/settings/general").text
    resp = client.post("/settings/general/api-key")
    assert resp.headers["HX-Redirect"] == "/settings/general?saved=key"
    new = auth.api_key(settings.config_dir)
    assert new != old and new in client.get("/settings/general").text
    assert client.app.state.logs.redactor.secrets[-1] == new


def test_auth_mode_choice(client):
    form = {
        "auth__mode": "local",
        "log__level": "info",
        "backup__interval_days": "7",
        "backup__keep": "7",
    }
    client.post("/settings/general", data=form)
    assert setting(client, "auth.mode") == "local"
    bad = client.post("/settings/general", data=form | {"auth__mode": "anyone"})
    assert bad.status_code == 422 and setting(client, "auth.mode") == "local"


def test_first_weekday_changes_the_calendar(client):
    form = {
        "ui__first_weekday": "6",
        "ui__week_header": "ddd m/d",
        "ui__short_date": "iso",
        "ui__long_date": "day-mon-d-y",
        "ui__time_format": "24",
        "ui__theme": "auto",
    }
    assert client.post("/settings/ui", data=form, follow_redirects=False).status_code == 303
    page = client.get("/calendar").text
    assert page.index('title="Sunday"') < page.index('title="Monday"')


# Profiles


def test_profiles_create_edit_delete(client, mags):
    resp = client.post(
        "/settings/profiles",
        data={"name": "Comics", "formats": ["cbz", "pdf"], "rank_cbz": "1", "rank_pdf": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session(client) as s:
        comics = s.scalars(select(FormatProfile).where(FormatProfile.name == "Comics")).one()
        assert comics.formats == ["cbz", "pdf"] and comics.prefer_true_pdf is False
        comics_id = comics.id
    edit = client.get(f"/settings/profiles?edit={comics_id}").text
    assert "Edit Comics" in edit
    client.post(
        "/settings/profiles",
        data={
            "id": str(comics_id),
            "name": "Comics",
            "formats": ["cbz", "pdf"],
            "rank_cbz": "2",
            "rank_pdf": "1",
            "prefer_true_pdf": "on",
        },
    )
    with session(client) as s:
        comics = s.get(FormatProfile, comics_id)
        assert comics.formats == ["pdf", "cbz"] and comics.prefer_true_pdf
        default = s.scalars(select(FormatProfile).where(FormatProfile.id != comics_id)).first()
        s.get(Magazine, mags["People"]).format_profile_id = comics_id
        s.commit()
    clash = client.post("/settings/profiles", data={"name": default.name, "formats": ["pdf"]})
    assert clash.status_code == 422 and "Another profile" in clash.text
    empty = client.post("/settings/profiles", data={"name": "None", "formats": []})
    assert empty.status_code == 422 and "at least one format" in empty.text
    assert toast(client.post(f"/settings/profiles/{comics_id}/delete"))["kind"] == "warn"
    with session(client) as s:
        s.get(Magazine, mags["People"]).format_profile_id = None
        s.commit()
    assert client.post(f"/settings/profiles/{comics_id}/delete").headers["HX-Redirect"]
    with session(client) as s:
        assert s.get(FormatProfile, comics_id) is None


# Tags


def test_tags_rename_and_delete(client, mags):
    with session(client) as s:
        s.get(Magazine, mags["Fortean Times"]).tags = ["weird", "print"]
        s.get(Magazine, mags["People"]).tags = ["print"]
        s.commit()
    page = client.get("/settings/tags").text
    assert "weird" in page and ">2<" in page.replace(" ", "")
    client.post("/settings/tags/rename", data={"old": "print", "new": "paper"})
    with session(client) as s:
        assert s.get(Magazine, mags["Fortean Times"]).tags == ["weird", "paper"]
        assert s.get(Magazine, mags["People"]).tags == ["paper"]
    client.post("/settings/tags/delete", data={"tag": "paper"})
    with session(client) as s:
        assert s.get(Magazine, mags["People"]).tags == []
    assert (
        toast(client.post("/settings/tags/rename", data={"old": "weird", "new": ""}))["kind"]
        == "warn"
    )
