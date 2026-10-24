"""Webhook notifications: queued on commit, sent to enabled entries, never secrets."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from fake_webhook import HOOK_URL, FakeWebhook
from periodicool import downloads, jobs, notify
from periodicool.library import set_setting
from periodicool.models import Magazine, Notification

from fake_prowlarr import KEY
from test_downloads import L311, TODAY, finish, seen
from test_downloads import env as env  # noqa: F401  (fixture)

ALL = list(notify.EVENTS)


@pytest.fixture
def hook(env):
    fake = FakeWebhook()
    notify.start(sessionmaker(env.session.bind, expire_on_commit=False), fake, inline=True)
    yield fake
    notify.stop()


def add_hook(e, events=ALL, **kw):
    row = Notification(name=kw.pop("name", "Hook"), url=HOOK_URL, events=events, **kw)
    e.session.add(row)
    e.session.commit()
    return row


def test_grab_and_import_send_after_commit(env, hook):
    e = env
    add_hook(e)
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    assert hook.calls == []  # not before the commit
    e.session.commit()
    [grab] = [c["json"] for c in hook.calls]
    assert grab["eventType"] == "Grab" and grab["instanceName"] == "PeriodiCool"
    assert grab["magazine"]["title"] == "Linux Magazine" and grab["issue"]["key"] == "n311"
    assert grab["release"]["title"] == L311 and grab["release"]["downloadClient"] == "SABnzbd"
    assert grab["release"]["downloadId"] == g.job_id and grab["release"]["protocol"] == "usenet"
    finish(e, g, {"Linux311.pdf": b"magazine"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    e.session.commit()
    imported = hook.calls[-1]["json"]
    assert imported["eventType"] == "Download"
    assert imported["file"]["path"].startswith("Linux Magazine/") and imported["file"]["format"]
    text = str(hook.calls)
    assert KEY not in text and "apikey" not in text and str(e.settings.library_dir) not in text


def test_rollback_sends_nothing(env, hook):
    e = env
    add_hook(e)
    downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY)
    e.session.rollback()
    assert hook.calls == []


def test_only_enabled_entries_and_wanted_events(env, hook):
    e = env
    add_hook(e, events=["Download"], name="Imports only")
    add_hook(e, name="Off", enabled=False)
    downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY)
    e.session.commit()
    assert hook.calls == []


def test_failure_and_review_events_carry_the_reason(env, hook):
    e = env
    add_hook(e)
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    e.session.commit()
    downloads.fail(e.session, g, "download failed: missing articles", None, None, TODAY)
    e.session.commit()
    failed = hook.calls[-1]["json"]
    assert failed["eventType"] == "DownloadFailure"
    assert failed["reason"] == "download failed: missing articles"


def test_magazine_add_and_delete(env, hook):
    from periodicool import magazines

    e = env
    add_hook(e)
    mag = e.session.scalars(select(Magazine).where(Magazine.title == "Linux Magazine")).one()
    magazines.delete_magazines(e.session, [mag.id])
    e.session.commit()
    assert hook.events() == ["MagazineDelete"]
    assert hook.calls[0]["json"]["magazine"]["title"] == "Linux Magazine"


def test_auth_headers_put_and_application_url(env, hook):
    e = env
    set_setting(e.session, notify.APPLICATION_URL_KEY, "https://mags.example")
    add_hook(
        e,
        username="me",
        password="hook-pass-1",
        settings={"method": "PUT", "headers": [["X-Token", "tok-123"], ["Host", "evil"]]},
    )
    mag = e.session.scalars(select(Magazine).where(Magazine.title == "Linux Magazine")).one()
    notify.magazine_event(e.session, "MagazineAdd", mag)
    e.session.commit()
    [call] = hook.calls
    assert call["method"] == "PUT" and call["headers"]["X-Token"] == "tok-123"
    assert "Host" not in call["headers"] and call["headers"]["Authorization"].startswith("Basic ")
    assert call["json"]["applicationUrl"] == "https://mags.example"
    assert "hook-pass-1" not in str(call["json"])


def test_failures_are_noted_and_never_break_the_grab(env, hook, caplog):
    e = env
    row = add_hook(e, password="hook-pass-1")
    hook.status = 500
    res = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY)
    e.session.commit()
    assert res.ok
    e.session.refresh(row)
    assert row.last_status == "failed" and row.last_error == "answered 500"
    hook.fail_with = ConnectionRefusedError("refused hook-pass-1")
    notify.send_to_all(
        sessionmaker(e.session.bind), "MagazineAdd", {"eventType": "MagazineAdd"}, hook
    )
    e.session.refresh(row)
    assert "cannot reach" in row.last_error and "hook-pass-1" not in row.last_error
    assert "hook-pass-1" not in caplog.text


def test_redirects_are_reported_not_followed():
    row = Notification(name="x", url=HOOK_URL, events=ALL, settings={})
    sent, what = notify.deliver(row, "Test", {"eventType": "Test"}, lambda *a: 302)
    assert not sent and "redirect" in what


def test_health_job_sends_new_and_cleared_issues(env, hook, monkeypatch):
    from periodicool import health

    e = env
    row = add_hook(e, events=["Health", "HealthRestored"])
    state = {"checks": []}
    monkeypatch.setattr(health, "local_checks", lambda *a, **k: list(state["checks"]))
    monkeypatch.setattr(health, "source_checks", lambda *a, **k: [])
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    jobs.run_health(e.session, e.settings, now)  # first run only records
    e.session.commit()
    state["checks"] = [health.Check("Library folder", "bad", "missing", "Mount it.")]
    jobs.run_health(e.session, e.settings, now)
    e.session.commit()
    assert hook.events() == ["Health"]
    assert hook.calls[0]["json"]["health"] == {
        "name": "Library folder",
        "level": "bad",
        "message": "missing",
        "fix": "Mount it.",
    }
    # A warning is only sent when warnings are included.
    state["checks"].append(health.Check("Indexers", "warn", "none", ""))
    jobs.run_health(e.session, e.settings, now)
    e.session.commit()
    assert hook.events() == ["Health"]
    row.settings = {"include_health_warnings": True}
    state["checks"] = [health.Check("Library folder", "bad", "missing", "Mount it.")]
    e.session.commit()
    jobs.run_health(e.session, e.settings, now)  # the warning cleared
    e.session.commit()
    assert hook.events() == ["Health", "HealthRestored"]
