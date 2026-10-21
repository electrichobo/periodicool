import io
import os
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import ADMIN, csrf_of
from periodicool import auth, cli
from periodicool.library import set_setting
from periodicool.main import create_app
from periodicool.models import LoginSession, User

# Hashing and secrets


def test_password_hash_round_trip():
    stored = auth.hash_password("hunter2hunter2")
    assert stored.startswith("scrypt$") and "hunter2" not in stored
    assert auth.verify_password("hunter2hunter2", stored)
    assert not auth.verify_password("hunter2hunter3", stored)
    assert stored != auth.hash_password("hunter2hunter2")  # salted


@pytest.mark.parametrize("stored", ["", "plain", "md5$a$b$c$d$e", "scrypt$x$8$1$AA==$AA=="])
def test_verify_rejects_malformed_hashes(stored):
    assert not auth.verify_password("anything", stored)


def test_password_rules():
    assert auth.password_problem("short", "short")
    assert auth.password_problem("long enough", "long enougH")
    assert auth.password_problem("long enough", "long enough") is None


def test_secret_file_is_created_once_and_private(tmp_path):
    first = auth.load_secret(tmp_path, env={})
    assert len(first) == 64 and auth.load_secret(tmp_path, env={}) == first
    if os.name == "posix":
        assert (tmp_path / auth.SECRET_FILE).stat().st_mode & 0o777 == 0o600


def test_secret_from_environment_wins(tmp_path):
    assert auth.load_secret(tmp_path, env={auth.SECRET_ENV: "from-env"}) == b"from-env"
    assert not (tmp_path / auth.SECRET_FILE).exists()


def test_api_key_create_regenerate_and_match(tmp_path):
    key = auth.api_key(tmp_path)
    assert len(key) == 32 and auth.api_key(tmp_path) == key
    assert auth.api_key_matches(tmp_path, key)
    assert not auth.api_key_matches(tmp_path, None) and not auth.api_key_matches(tmp_path, "x")
    new = auth.regenerate_api_key(tmp_path)
    assert new != key and not auth.api_key_matches(tmp_path, key)


def test_app_start_creates_secret_and_api_key(client, settings):
    assert (settings.config_dir / auth.SECRET_FILE).exists()
    assert (settings.config_dir / auth.API_KEY_FILE).exists()


# Addresses and backoff


def test_local_addresses():
    for address in ["127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "172.16.3.4", "fe80::1"]:
        assert auth.is_local(address), address
    for address in ["8.8.8.8", "2001:4860::8888", "testclient", ""]:
        assert not auth.is_local(address), address


def test_forwarded_for_only_with_trust_proxy():
    assert auth.client_address("172.17.0.1", "8.8.8.8, 10.0.0.1", False) == "172.17.0.1"
    assert auth.client_address("172.17.0.1", "8.8.8.8, 10.0.0.1", True) == "8.8.8.8"
    assert auth.client_address("172.17.0.1", None, True) == "172.17.0.1"


def test_login_throttle_doubles_and_caps():
    now = [0.0]
    throttle = auth.LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        throttle.failed("a")
    assert throttle.wait_left("a") == 0
    throttle.failed("a")
    assert throttle.wait_left("a") == 30
    throttle.failed("a")
    assert throttle.wait_left("a") == 60
    for _ in range(10):
        throttle.failed("a")
    assert throttle.wait_left("a") == 15 * 60
    assert throttle.wait_left("b") == 0
    throttle.succeeded("a")
    assert throttle.wait_left("a") == 0


# Setup and login over HTTP


def _setup(c: TestClient, password=ADMIN[1], confirm=None, username=ADMIN[0]):
    return c.post(
        "/setup",
        data={
            "csrf_token": csrf_of(c, "/setup"),
            "username": username,
            "password": password,
            "confirm": password if confirm is None else confirm,
        },
    )


def _login(c: TestClient, password=ADMIN[1], next_="/"):
    token = csrf_of(c, "/login")
    return c.post(
        "/login",
        data={"csrf_token": token, "username": ADMIN[0], "password": password, "next": next_},
    )


def test_first_visit_goes_to_setup(anon_client):
    resp = anon_client.get("/ui-kit")
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    assert anon_client.get("/login").headers["location"] == "/setup"
    assert anon_client.get("/health").status_code == 200
    assert anon_client.get("/static/css/theme.css").status_code == 200
    page = anon_client.get("/setup")
    assert page.status_code == 200 and "Create the admin login" in page.text
    assert 'class="nav"' not in page.text


def test_setup_validates(anon_client):
    assert "at least 8" in _setup(anon_client, password="short").text
    assert "do not match" in _setup(anon_client, confirm="something else").text
    assert "Enter a username" in _setup(anon_client, username=" ").text
    with anon_client.app.state.sessions() as db:
        assert not auth.has_user(db)


def test_setup_creates_admin_once_and_logs_in(anon_client):
    resp = _setup(anon_client)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.cookies.get(auth.SESSION_COOKIE)
    assert cookie
    assert anon_client.get("/ui-kit").status_code == 200
    # Setup is closed once a user exists, for GET and POST.
    assert anon_client.get("/setup").headers["location"] == "/login"
    anon_client.cookies.clear()
    token = csrf_of(anon_client, "/login")
    data = {"csrf_token": token, "username": "intruder", "password": "x" * 8, "confirm": "x" * 8}
    assert anon_client.post("/setup", data=data).headers["location"] == "/login"
    with anon_client.app.state.sessions() as db:
        assert [u.username for u in db.scalars(select(User))] == [ADMIN[0]]
        # Only the token's sha256 is stored.
        stored = db.scalars(select(LoginSession.token_hash)).all()
        assert stored and cookie not in stored and all(len(h) == 64 for h in stored)


def test_session_cookie_flags(anon_client):
    header = _setup(anon_client).headers["set-cookie"]
    assert "HttpOnly" in header and "SameSite=lax" in header and "Max-Age=2592000" in header


def test_login_logout_and_next(anon_client):
    _setup(anon_client)
    anon_client.cookies.clear()
    assert anon_client.get("/ui-kit").headers["location"] == "/login?next=%2Fui-kit"
    bad = _login(anon_client, password="wrong password")
    assert bad.status_code == 401 and "Wrong username or password" in bad.text
    good = _login(anon_client, next_="/ui-kit")
    assert good.status_code == 303 and good.headers["location"] == "/ui-kit"
    page = anon_client.get("/ui-kit")
    assert page.status_code == 200 and "Log out" in page.text
    token = csrf_of(anon_client, "/ui-kit")
    out = anon_client.post("/logout", data={"csrf_token": token})
    assert out.status_code == 303 and out.headers["location"] == "/login"
    anon_client.cookies.clear()
    assert anon_client.get("/ui-kit").status_code == 303


@pytest.mark.parametrize("target", ["//evil.example", "/\\evil.example", "https://evil.example"])
def test_login_next_stays_on_site(anon_client, target):
    _setup(anon_client)
    anon_client.cookies.clear()
    assert _login(anon_client, next_=target).headers["location"] == "/"


def test_failed_logins_slow_down(anon_client):
    _setup(anon_client)
    anon_client.cookies.clear()
    for _ in range(auth.FREE_ATTEMPTS):
        assert _login(anon_client, password="wrong password").status_code == 401
    locked = _login(anon_client)
    assert locked.status_code == 429 and "Too many failed logins" in locked.text


def test_logout_ends_session_server_side(anon_client):
    cookie = _setup(anon_client).cookies.get(auth.SESSION_COOKIE)
    anon_client.post("/logout", data={"csrf_token": csrf_of(anon_client, "/ui-kit")})
    anon_client.cookies.set(auth.SESSION_COOKIE, cookie)
    assert anon_client.get("/ui-kit").status_code == 303


def test_expired_session_is_refused(anon_client):
    _setup(anon_client)
    with anon_client.app.state.sessions() as db:
        from datetime import UTC, datetime, timedelta

        for row in db.scalars(select(LoginSession)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert anon_client.get("/ui-kit").status_code == 303
    with anon_client.app.state.sessions() as db:
        assert db.scalars(select(LoginSession)).all() == []


def test_htmx_requests_get_a_redirect_header(anon_client):
    resp = anon_client.get("/ui-kit/modal", headers={"HX-Request": "true"})
    assert resp.status_code == 401 and resp.headers["HX-Redirect"] == "/setup"


# CSRF


def _api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif inner := getattr(route, "original_router", None):  # FastAPI's included router
            yield from _api_routes(inner.routes)


def _unsafe_routes(app):
    for route in _api_routes(app.routes):
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            yield method, re.sub(r"\{[^}]+\}", "1", route.path)


def test_every_unsafe_route_needs_csrf(client):
    routes = list(_unsafe_routes(client.app))
    assert ("POST", "/login") in routes and ("POST", "/logout") in routes
    bare = TestClient(client.app, cookies=client.cookies, follow_redirects=False)
    for method, path in routes:
        resp = bare.request(method, path, data={"csrf_token": "wrong"})
        assert resp.status_code == 403, (method, path, resp.status_code)
        resp = bare.request(method, path)
        assert resp.status_code == 403, (method, path, resp.status_code)


def test_csrf_header_works_for_htmx(client):
    resp = client.post("/ui-kit/toast")
    assert resp.status_code == 200 and "HX-Trigger" in resp.headers


def test_csrf_token_is_tied_to_the_session(client, anon_client):
    _set_mode(anon_client.app, "off")
    other = anon_client.post(
        "/ui-kit/toast", headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]}
    )
    assert other.status_code == 403


# Auth modes


def _set_mode(app, mode, trust_proxy=False):
    with app.state.sessions() as db:
        set_setting(db, "auth.mode", mode)
        set_setting(db, "auth.trust_proxy", trust_proxy)
        db.commit()


def test_mode_off_needs_no_login(anon_client):
    _set_mode(anon_client.app, "off")
    assert anon_client.get("/ui-kit").status_code == 200
    # Forms still need CSRF.
    assert anon_client.post("/ui-kit/toast").status_code == 403
    token = csrf_of(anon_client, "/ui-kit")
    assert anon_client.post("/ui-kit/toast", headers={"X-CSRF-Token": token}).status_code == 200


def test_mode_local_lets_private_addresses_in(settings):
    app = create_app(settings)
    with TestClient(app, client=("192.168.1.50", 5000), follow_redirects=False) as lan:
        _setup(lan)
        lan.cookies.clear()
        _set_mode(app, "local")
        assert lan.get("/ui-kit").status_code == 200
    with TestClient(app, client=("8.8.8.8", 5000), follow_redirects=False) as wan:
        assert wan.get("/ui-kit").status_code == 303


def test_mode_local_ignores_forwarded_for_unless_trusted(settings):
    app = create_app(settings)
    with TestClient(app, client=("8.8.8.8", 5000), follow_redirects=False) as c:
        _setup(c)
        c.cookies.clear()
        _set_mode(app, "local")
        assert c.get("/ui-kit", headers={"X-Forwarded-For": "192.168.1.5"}).status_code == 303
        _set_mode(app, "local", trust_proxy=True)
        assert c.get("/ui-kit", headers={"X-Forwarded-For": "192.168.1.5"}).status_code == 200


def test_unknown_mode_falls_back_to_required(anon_client):
    _set_mode(anon_client.app, "bogus")
    assert anon_client.get("/ui-kit").status_code == 303


# CLI


def test_cli_reset_password(settings, monkeypatch, anon_client):
    out = io.StringIO()
    monkeypatch.setattr(cli, "READ_PASSWORD", lambda: ("brand new pass", "brand new pass"))
    assert cli.main(["user", "reset-password"], settings=settings, out=out) == 1
    assert "/setup" in out.getvalue()

    _setup(anon_client)
    out = io.StringIO()
    assert cli.main(["user", "reset-password"], settings=settings, out=out) == 0
    assert "Password changed for admin" in out.getvalue()
    # Old session is gone, old password fails, new one works.
    assert anon_client.get("/ui-kit").status_code == 303
    assert _login(anon_client).status_code == 401
    assert _login(anon_client, password="brand new pass").status_code == 303

    monkeypatch.setattr(cli, "READ_PASSWORD", lambda: ("short", "short"))
    assert cli.main(["user", "reset-password"], settings=settings, out=io.StringIO()) == 2
    monkeypatch.setattr(cli, "READ_PASSWORD", lambda: ("long enough!", "long enough!"))
    args = ["user", "reset-password", "--username", "nobody"]
    assert cli.main(args, settings=settings, out=io.StringIO()) == 1
