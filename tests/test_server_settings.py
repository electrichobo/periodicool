"""Port, URL base and allowed hosts: saved in Settings, General, applied at start."""

import re
from dataclasses import replace

from fastapi.testclient import TestClient

from conftest import ADMIN, csrf_of
from periodicool import db, library
from periodicool.config import (
    HOSTS_KEY,
    PORT_KEY,
    URL_BASE_KEY,
    load_settings,
    with_saved_server_settings,
)
from periodicool.main import create_app
from periodicool.web.urlbase import clean_base, rewrite_html


def save(settings, **values):
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    with db.make_session_factory(engine)() as s:
        for key, value in values.items():
            library.set_setting(s, key, value)
        s.commit()
    engine.dispose()


def env_for(tmp_path, **extra):
    return {
        "PERIODICOOL_CONFIG_DIR": str(tmp_path / "config"),
        "PERIODICOOL_LIBRARY_DIR": str(tmp_path / "library"),
        "PERIODICOOL_JOBS": "off",
        **extra,
    }


def test_saved_values_apply_unless_the_environment_sets_them(tmp_path):
    settings = load_settings(env_for(tmp_path))
    save(settings, **{PORT_KEY: 8484, URL_BASE_KEY: "/mags", HOSTS_KEY: ["deck.example"]})
    applied = with_saved_server_settings(settings)
    assert (applied.port, applied.url_base, applied.allowed_hosts) == (
        8484,
        "/mags",
        ("deck.example",),
    )
    forced = with_saved_server_settings(
        load_settings(env_for(tmp_path, PERIODICOOL_PORT="9000", PERIODICOOL_URL_BASE=""))
    )
    assert forced.port == 9000 and forced.url_base == "/mags"
    forced = with_saved_server_settings(
        load_settings(env_for(tmp_path, PERIODICOOL_URL_BASE="other/"))
    )
    assert forced.url_base == "/other"


def test_clean_base_and_rewrite():
    assert clean_base("") == "" and clean_base(" /a/b/ ") == "/a/b" and clean_base("x") == "/x"
    html = (
        b'<a href="/settings">S</a><a href="//cdn">x</a><a href="https://e/">e</a>'
        b'<button hx-post="/tasks/rss" data-browse-to="/mnt/data"></button>'
        b'<tr data-open="/issues/3">'
    )
    out = rewrite_html(html, "/pc").decode()
    assert 'href="/pc/settings"' in out and 'hx-post="/pc/tasks/rss"' in out
    assert 'data-open="/pc/issues/3"' in out
    assert 'href="//cdn"' in out and 'href="https://e/"' in out
    assert 'data-browse-to="/mnt/data"' in out  # a folder, not a URL


def test_app_under_a_url_base(tmp_path):
    settings = load_settings(env_for(tmp_path, PERIODICOOL_URL_BASE="/periodicool"))
    with TestClient(create_app(settings)) as c:
        assert c.get("/health").json()["status"] == "ok"  # container check at the root
        assert c.get("/", follow_redirects=False).headers["location"] == "/periodicool/"
        assert c.get("/settings").status_code == 404
        setup = c.get("/periodicool/setup")
        assert setup.status_code == 200
        token = re.search(r'name="csrf_token" value="([^"]+)"', setup.text).group(1)
        resp = c.post(
            "/periodicool/setup",
            data={
                "csrf_token": token,
                "username": ADMIN[0],
                "password": ADMIN[1],
                "confirm": ADMIN[1],
            },
            follow_redirects=False,
        )
        assert resp.headers["location"].startswith("/periodicool/")
        page = c.get("/periodicool/").text
        assert (
            'href="/periodicool/calendar"' in page
            and 'href="/periodicool/static/css/theme.css' in page
        )
        assert 'href="/calendar"' not in page
        assert '<meta name="url-base" content="/periodicool">' in page
        assert c.get("/periodicool/static/css/theme.css").status_code == 200
        moved = c.get("/periodicool/library", follow_redirects=False)
        assert moved.headers["location"] == "/periodicool/import"


def test_allowed_hosts(tmp_path):
    settings = load_settings(env_for(tmp_path, PERIODICOOL_ALLOWED_HOSTS="deck.example"))
    app = create_app(settings)
    with TestClient(app, base_url="http://deck.example") as ok:
        assert ok.get("/health").status_code == 200
    with TestClient(app, base_url="http://localhost") as local:
        assert local.get("/health").status_code == 200
    with TestClient(app, base_url="http://evil.example") as other:
        assert other.get("/health").status_code == 400


def test_general_tab_saves_server_settings(client):
    form = {
        "auth__mode": "required",
        "log__level": "info",
        "server__port": "8484",
        "server__url_base": "periodicool/",
        "server__allowed_hosts": "deck.example, nas.local",
    }
    resp = client.post("/settings/general", data=form, follow_redirects=False)
    assert resp.status_code == 303
    with client.app.state.sessions() as s:
        assert library.get_setting(s, PORT_KEY) == 8484
        assert library.get_setting(s, URL_BASE_KEY) == "/periodicool"
        assert library.get_setting(s, HOSTS_KEY) == ["deck.example", "nas.local"]
    bad = client.post("/settings/general", data=form | {"server__url_base": "/a b"})
    assert bad.status_code == 422 and "Use a path such as /periodicool" in bad.text
    assert csrf_of  # imported for the setup helper elsewhere


def test_settings_replace_keeps_env_flags(tmp_path):
    settings = load_settings(env_for(tmp_path, PERIODICOOL_PORT="9001"))
    assert "PERIODICOOL_PORT" in replace(settings, port=1).from_env
