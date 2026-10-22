"""Settings tabs over HTTP. Secrets never appear; invalid input saves nothing."""

import pytest
from sqlalchemy import select

from periodicool import auth, downloads, library, search
from periodicool.models import FormatProfile, Job, Magazine

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
    for tab in ("indexers", "downloads", "general"):
        text = client.get(f"/settings/{tab}").text
        assert "prowlarr-secret-value" not in text and "sab-secret-value" not in text


def test_save_indexers_converts_units_and_updates_jobs(client):
    with session(client) as s:
        s.add(Job(name="rss", enabled=True, interval_s=900))
        s.commit()
    form = {
        "prowlarr__url": "http://prowlarr:9696/",
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
    assert setting(client, "prowlarr.url") == "http://prowlarr:9696"
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
        ("prowlarr__url", "prowlarr:9696", "full address"),
        ("prowlarr__url", "http://u:p@prowlarr:9696", "Leave keys"),
        ("prowlarr__url", "http://prowlarr:9696/?apikey=x", "Leave keys"),
        ("search__categories", "magazines", "Numbers separated"),
        ("search__cache_hours", "500", "At most 168"),
        ("jobs__rss__interval_s", "1", "At least 5"),
        ("search__early_days", "soon", "whole number"),
    ],
)
def test_bad_input_saves_nothing(client, field, value, message):
    form = {
        "prowlarr__url": "http://prowlarr:9696",
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
    assert setting(client, "prowlarr.url") is None


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


def test_downloads_path_map(client):
    base = {
        "sabnzbd__url": "http://sab:8080",
        "sabnzbd__category": "mags",
        "grab__max_per_run": "10",
        "jobs__downloads__interval_s": "60",
    }
    resp = client.post(
        "/settings/downloads",
        data=base | {"map_remote": ["/data/complete/mags", ""], "map_local": ["/downloads/", ""]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert setting(client, downloads.PATH_MAP_KEY) == [["/data/complete/mags", "/downloads"]]
    half = client.post(
        "/settings/downloads", data=base | {"map_remote": ["/data"], "map_local": [""]}
    )
    assert half.status_code == 422 and "needs both" in half.text
    assert setting(client, downloads.PATH_MAP_KEY) == [["/data/complete/mags", "/downloads"]]
    assert setting(client, "grab.auto") is False


def test_connection_tests_without_setup(client):
    assert "Set the Prowlarr URL" in client.post("/settings/indexers/test").text
    assert toast(client.post("/settings/downloads/test"))["kind"] == "warn"


def test_connection_tests_with_fakes(client, fakes):
    page = client.post("/settings/indexers/test").text
    assert "Connected" in page
    assert "answered" in toast(client.post("/settings/downloads/test"))["text"]


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
    client.post("/settings/ui", data={"ui__first_weekday": "6"})
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
