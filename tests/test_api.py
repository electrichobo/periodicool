"""The REST API under /api/v1: key auth, Sonarr-shaped JSON, writes and commands."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from periodicool import auth
from periodicool.main import create_app


@pytest.fixture
def api(settings):
    """A client with no login session, sending the API key."""
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.headers["X-Api-Key"] = auth.api_key(settings.config_dir)
        yield c


def _api_routes():
    from periodicool.web.api import router

    for route in router.routes:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            yield method, re.sub(r"\{[^}]+\}", "1", route.path)


def test_every_route_needs_the_key(api, settings):
    routes = list(_api_routes())
    assert ("GET", "/api/v1/magazine") in routes and ("POST", "/api/v1/command") in routes
    bare = TestClient(api.app, follow_redirects=False)
    for method, path in routes:
        resp = bare.request(method, path)
        assert resp.status_code == 401, (method, path)
        assert resp.json() == {"message": "API key missing or wrong"}
        wrong = bare.request(method, path, headers={"X-Api-Key": "nope"})
        assert wrong.status_code == 401, (method, path)
    key = auth.api_key(settings.config_dir)
    assert bare.get(f"/api/v1/system/status?apikey={key}").status_code == 200


def test_a_login_session_alone_is_not_enough(client):
    assert client.get("/api/v1/system/status").status_code == 401


def test_writes_need_no_csrf_but_web_routes_still_do(api):
    resp = api.post("/api/v1/magazine", json={"title": "Wired", "frequency": "monthly"})
    assert resp.status_code == 201
    # The API key opens the API only, never the web routes.
    assert api.post("/logout").status_code in (401, 403)


def test_no_cdn_docs_and_a_keyed_schema(api):
    bare = TestClient(api.app, follow_redirects=False)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.get(path).status_code in (303, 404), path
    assert bare.get("/api/v1/openapi.json").status_code == 401
    schema = api.get("/api/v1/openapi.json").json()
    assert "/api/v1/magazine" in schema["paths"]
    assert not any(p.startswith("/settings") for p in schema["paths"])


def test_status_and_health(api):
    status = api.get("/api/v1/system/status").json()
    assert status["appName"] == "PeriodiCool" and status["version"]
    assert status["startTime"].endswith("+00:00")
    checks = api.get("/api/v1/health").json()
    assert checks and {"name", "level", "message", "fix"} <= set(checks[0])


def test_magazine_add_edit_issues_and_delete(api, settings):
    bad = api.post("/api/v1/magazine", json={"title": "X", "frequency": "sometimes"})
    assert bad.status_code == 400 and "frequency" in bad.json()["errors"]
    unknown = api.post("/api/v1/magazine", json={"title": "X", "colour": "red"})
    assert unknown.status_code == 400 and "colour" in unknown.json()["message"]
    added = api.post(
        "/api/v1/magazine",
        json={
            "title": "Linux Magazine",
            "aliases": ["Linux Pro Magazine"],
            "frequency": "monthly",
            "numbering": "issue",
            "anchorNumber": 311,
            "anchorDate": "2026-10-01",
            "monitorMode": "all",
        },
    )
    assert added.status_code == 201, added.text
    mag = added.json()
    assert mag["title"] == "Linux Magazine" and mag["aliases"] == ["Linux Pro Magazine"]
    assert mag["statistics"]["issueCount"] > 0
    assert api.post("/api/v1/magazine", json={"title": "linux magazine"}).status_code == 409
    edited = api.put(f"/api/v1/magazine/{mag['id']}", json={"monitored": False, "graceDays": 30})
    assert edited.status_code == 200, edited.text
    assert edited.json()["monitored"] is False and edited.json()["graceDays"] == 30
    assert edited.json()["aliases"] == ["Linux Pro Magazine"]
    assert [m["id"] for m in api.get("/api/v1/magazine").json()] == [mag["id"]]
    issues = api.get(f"/api/v1/issue?magazineId={mag['id']}").json()
    first = issues[0]
    assert {"key", "coverDate", "issueNumber", "monitored", "state", "file"} <= set(first)
    flipped = api.put(f"/api/v1/issue/{first['id']}", json={"monitored": not first["monitored"]})
    assert flipped.json()["monitored"] is (not first["monitored"])
    assert api.put(f"/api/v1/issue/{first['id']}", json={"key": "x"}).status_code == 400
    assert api.get("/api/v1/magazine/999").status_code == 404
    (settings.library_dir / "Linux Magazine").mkdir(parents=True)
    keep = settings.library_dir / "Linux Magazine" / "a.pdf"
    keep.write_bytes(b"pdf")
    assert api.delete(f"/api/v1/magazine/{mag['id']}").status_code == 200
    assert api.get("/api/v1/magazine").json() == [] and keep.exists()


def test_lists_are_paged_and_carry_no_secrets(api, settings):
    api.post("/api/v1/magazine", json={"title": "Wired", "frequency": "monthly"})
    for path in ("/api/v1/wanted/missing", "/api/v1/history"):
        page = api.get(path).json()
        assert {"page", "pageSize", "totalPages", "totalRecords", "records"} <= set(page)
    assert api.get("/api/v1/queue").json() == {"totalRecords": 0, "records": []}
    assert isinstance(api.get("/api/v1/calendar?start=2026-01-01&end=2026-12-31").json(), list)
    assert api.get("/api/v1/calendar?start=2026-12-01&end=2026-01-01").status_code == 400
    assert api.get("/api/v1/history?eventType=bogus").status_code == 400
    everything = "".join(
        api.get(p).text
        for p in ("/api/v1/magazine", "/api/v1/wanted/missing", "/api/v1/system/status")
    )
    assert str(settings.config_dir) not in everything
    assert str(settings.library_dir) not in everything
    assert auth.api_key(settings.config_dir) not in everything


def _wait(api, command_id):
    for _ in range(250):
        cmd = api.get(f"/api/v1/command/{command_id}").json()
        if cmd["status"] in ("completed", "failed"):
            return cmd
        time.sleep(0.02)
    raise AssertionError("command did not finish")


def test_commands_run_through_the_task_runners(api):
    from periodicool import jobs

    calls = []

    def ok(session, settings, now):
        calls.append("health")
        return "all good"

    def boom(session, settings, now):
        raise RuntimeError("disk on fire")

    api.app.state.job_runners = {**jobs.RUNNERS, "health": ok, "rss": boom}
    started = api.post("/api/v1/command", json={"name": "HealthCheck"})
    assert started.status_code == 201
    done = _wait(api, started.json()["id"])
    assert done["status"] == "completed" and done["message"] == "all good"
    assert calls == ["health"]
    failed = _wait(api, api.post("/api/v1/command", json={"name": "RssSync"}).json()["id"])
    assert failed["status"] == "failed" and "disk on fire" in failed["message"]
    assert api.post("/api/v1/command", json={"name": "Nuke"}).status_code == 400
    assert api.post("/api/v1/command", json={"name": "MagazineSearch"}).status_code == 400
    missing = api.post("/api/v1/command", json={"name": "MagazineSearch", "magazineId": 99})
    assert missing.status_code == 404
    names = [c["name"] for c in api.get("/api/v1/command").json()]
    assert names[:2] == ["RssSync", "HealthCheck"]


def test_magazine_search_without_indexers_fails_with_a_reason(api):
    mag = api.post("/api/v1/magazine", json={"title": "Wired", "frequency": "monthly"}).json()
    started = api.post("/api/v1/command", json={"name": "MagazineSearch", "magazineId": mag["id"]})
    done = _wait(api, started.json()["id"])
    assert done["status"] == "failed" and "no indexer" in done["message"]


def test_old_commands_are_dropped():
    from periodicool import api_commands

    cmds = api_commands.Commands(lambda: None, lambda mid: "ok")
    try:
        ids = [cmds.submit("MagazineSearch", 1).id for _ in range(api_commands.KEEP + 5)]
        assert cmds.get(ids[0]) is None and cmds.get(ids[-1]) is not None
        assert len(cmds.recent()) == api_commands.KEEP
    finally:
        cmds.stop()
