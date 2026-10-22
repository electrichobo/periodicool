"""System pages, backups, health checks, tasks, events, log files and the Library page."""

import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from periodicool import backup, health, library
from periodicool.config import DB_FILENAME
from periodicool.models import ImportItem, ItemAction, Job, LogEvent, Magazine

from test_web_magazines import fakes, mags, session, toast  # noqa: F401


@pytest.mark.parametrize("tab", ["status", "health", "tasks", "backups", "events", "logs"])
def test_every_system_tab_renders(client, tab):
    page = client.get(f"/system/{tab}")
    assert page.status_code == 200 and 'aria-label="System"' in page.text


def test_status_page(client):
    page = client.get("/system/status").text
    assert "GPL-3.0" in page and "no telemetry" in page and "Uptime" in page
    assert client.get("/system", follow_redirects=False).headers["location"] == "/system/status"


# Health


def test_local_health_checks(client, settings):
    page = client.get("/system/health").text
    assert "Prowlarr" in page and "Not set up." in page
    assert "does not exist" in page  # the test library folder was never made
    assert "first library scan" in page


def test_clock_check():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    ok = health.clock_check(
        "Prowlarr", "http://p", now=now, fetch=lambda u: now - timedelta(seconds=30)
    )
    assert ok.kind == "ok"
    bad = health.clock_check(
        "Prowlarr", "http://p", now=now, fetch=lambda u: now + timedelta(days=5)
    )
    assert bad.kind == "bad" and "behind" in bad.message and "NTP" in bad.fix
    unknown = health.clock_check("SABnzbd", "http://s", now=now, fetch=lambda u: None)
    assert unknown.kind == "warn"
    assert health.clock_check("SABnzbd", None).kind == "info"


def test_connection_checks_use_the_services(client, fakes):
    client.app.state.clock_fetch = lambda url: datetime.now(UTC) - timedelta(hours=2)
    page = client.post("/system/health/connections").text
    assert "Answered" in page and "category exists" in page
    assert "120 minutes ahead of Prowlarr" in page or "minutes ahead of" in page


# Tasks


def test_tasks_list_and_run_now(client):
    calls = []

    def fake(session, settings, now):
        calls.append(now)
        return "did it"

    client.app.state.job_runners = {"rss": fake}
    page = client.get("/system/tasks").text
    for label in ("RSS sync", "Check downloads", "Backup", "Library rescan"):
        assert label in page
    resp = client.post("/system/tasks/rss/run")
    assert toast(resp)["text"] == "RSS sync: did it" and len(calls) == 1
    with session(client) as s:
        job = s.get(Job, "rss")
        assert job.last_result == "did it" and job.next_run_at is not None
    assert client.post("/system/tasks/bogus/run").status_code == 404


def test_backup_task_runs_for_real(client, settings):
    client.app.state.job_runners = None
    resp = client.post("/system/tasks/backup/run")
    assert "made periodicool-" in toast(resp)["text"]
    assert backup.list_backups(settings.config_dir)


# Backups


def test_backup_download_and_contents(client, settings):
    client.post("/system/backups")
    [made] = backup.list_backups(settings.config_dir)
    assert made.name in client.get("/system/backups").text
    resp = client.get(f"/system/backups/{made.name}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    path = settings.config_dir / "dl.zip"
    path.write_bytes(resp.content)
    assert set(zipfile.ZipFile(path).namelist()) == {DB_FILENAME, "secret.key", "api.key"}
    assert client.get("/system/backups/../secret.key").status_code == 404
    assert client.get("/system/backups/periodicool-x.zip").status_code == 404


def test_backups_are_pruned(settings, client):
    for _ in range(4):
        backup.create_backup(settings.config_dir, keep=2)
    assert len(backup.list_backups(settings.config_dir)) == 2


def test_restore_round_trip(client, settings):
    with session(client) as s:
        s.add(Magazine(title="Keep Me", frequency="monthly", numbering="date"))
        s.commit()
    client.post("/system/backups")
    [made] = backup.list_backups(settings.config_dir)
    with session(client) as s:
        s.delete(s.scalars(select(Magazine)).one())
        s.add(Magazine(title="Added Later", frequency="monthly", numbering="date"))
        s.commit()
    resp = client.post(f"/system/backups/{made.name}/restore")
    assert "Restored" in toast(resp)["text"]
    with session(client) as s:
        assert [m.title for m in s.scalars(select(Magazine))] == ["Keep Me"]
    names = [b.name for b in backup.list_backups(settings.config_dir)]
    assert any(n.endswith("-prerestore.zip") for n in names)
    # The safety copy holds the state from just before the restore.
    pre = next(n for n in names if n.endswith("-prerestore.zip"))
    with zipfile.ZipFile(settings.config_dir / "backups" / pre) as zf:
        assert DB_FILENAME in zf.namelist()
    # The app keeps working: pages render and forms still pass CSRF.
    assert client.get("/").status_code == 200
    assert client.post("/ui-kit/toast").status_code == 200


def test_bad_backup_is_refused(client, settings):
    folder = settings.config_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    junk = folder / "periodicool-20260101-000000-000000.zip"
    junk.write_bytes(b"not a zip")
    assert "not a zip" in toast(client.post(f"/system/backups/{junk.name}/restore"))["text"]
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("readme.txt", "x")
    assert "no database" in toast(client.post(f"/system/backups/{junk.name}/restore"))["text"]
    assert client.get("/").status_code == 200


# Events and log files


def test_events_filter_and_paging(client):
    with session(client) as s:
        for n in range(60):
            s.add(LogEvent(level="INFO", logger="periodicool.search", message=f"info {n}"))
        s.add(LogEvent(level="WARNING", logger="periodicool.downloads", message="careful <b>"))
        s.add(LogEvent(level="ERROR", logger="periodicool.jobs", message="broke"))
        s.commit()
    page = client.get("/system/events").text
    assert 'class="pager"' in page and "careful &lt;b&gt;" in page
    warn = client.get("/system/events?level=warning").text
    assert "careful" in warn and "broke" in warn and "info 3" not in warn


def test_log_files(client, settings):
    folder = settings.config_dir / "logs"
    folder.mkdir(exist_ok=True)
    (folder / "periodicool.txt").write_text("".join(f"line {n}\n" for n in range(500)))
    (settings.config_dir / "secret.txt").write_text("nope")
    assert "periodicool.txt" in client.get("/system/logs").text
    view = client.get("/system/logs/periodicool.txt?lines=20").text
    assert "line 499" in view and "line 400" not in view
    assert client.get("/system/logs/periodicool.txt/download").text.startswith("line 0")
    assert client.get("/system/logs/secret.txt").status_code == 404


# Library


def test_library_scan_preview_and_manual_import(client, settings, mags):
    lib = settings.library_dir
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "Fortean.Times.Issue.351.TruePDF-March.2017.pdf").write_bytes(b"ft")
    (lib / "mystery-file.pdf").write_bytes(b"??")
    page = client.get("/library").text
    assert "First scan" in page and "Manual import (0)" in page
    resp = client.post("/library/scan")
    target = resp.headers["HX-Redirect"]
    preview = client.get(target).text
    assert (
        "Fortean Times/2017/Fortean Times - 2017-03.pdf" in preview
        and "mystery-file.pdf" in preview
    )
    assert (lib / "Fortean.Times.Issue.351.TruePDF-March.2017.pdf").exists()  # preview only

    with session(client) as s:
        item = s.scalars(select(ImportItem).where(ImportItem.action == ItemAction.REVIEW)).one()
        item_id = item.id
    assert "Manual import (1)" in client.get("/library").text
    bad = client.post(f"/library/queue/{item_id}/resolve", data={"magazine_id": "", "key": ""})
    assert toast(bad)["kind"] == "warn"
    wrong = client.post(
        f"/library/queue/{item_id}/resolve",
        data={"magazine_id": str(mags["Fortean Times"]), "key": "not-a-key"},
    )
    assert toast(wrong)["kind"] == "bad"
    ok = client.post(
        f"/library/queue/{item_id}/resolve",
        data={"magazine_id": str(mags["Fortean Times"]), "key": "2017-04"},
    )
    assert toast(ok)["kind"] == "ok"
    assert (lib / "Fortean Times/2017/Fortean Times - 2017-04.pdf").read_bytes() == b"??"
    assert not (lib / "mystery-file.pdf").exists()


def test_library_skip(client, settings, mags):
    lib = settings.library_dir
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "mystery-file.pdf").write_bytes(b"??")
    client.post("/library/scan")
    with session(client) as s:
        item_id = s.scalars(select(ImportItem.id)).one()
    assert "Skipped" in toast(client.post(f"/library/queue/{item_id}/skip"))["text"]
    assert (lib / "mystery-file.pdf").exists()
    assert "Manual import (0)" in client.get("/library").text


# Console


def _events(client, *rows):
    with session(client) as s:
        for level, message in rows:
            s.add(LogEvent(level=level, logger="periodicool.downloads", message=message))
        s.commit()
        return max(e.id for e in s.scalars(select(LogEvent)))


def test_console_page_and_new_lines(client):
    last = _events(client, ("INFO", "first"), ("DEBUG", "noise"), ("WARNING", "careful <b>"))
    page = client.get("/system/console").text
    assert 'role="log"' in page and "first" in page and "noise" not in page
    assert "careful &lt;b&gt;" in page and f'value="{last}"' in page
    newer = _events(client, ("ERROR", "Not grabbed X: NZB download failed"))
    lines = client.get(f"/system/console/lines?after={last}&level=INFO").text
    assert "NZB download failed" in lines and "first" not in lines
    assert f'id="console-after" name="after" value="{newer}" hx-swap-oob="true"' in lines
    debug = client.get("/system/console/lines?after=0&level=DEBUG").text
    assert "noise" in debug
    warn = client.get("/system/console/lines?after=0&level=WARNING").text
    assert "first" not in warn and "careful" in warn


def test_verbose_switch_turns_itself_off(client):
    import logging
    from datetime import UTC, datetime, timedelta

    from periodicool.web.system import VERBOSE_KEY, apply_verbose

    logger = logging.getLogger("periodicool")
    resp = client.post("/system/console/verbose", data={"verbose": "on"})
    assert "one hour" in toast(resp)["text"]
    assert client.app.state.logs.verbose and logger.level == logging.DEBUG
    assert "Debug lines until" in client.get("/system/console").text
    with session(client) as s:
        library.set_setting(s, VERBOSE_KEY, (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        s.commit()
    assert apply_verbose(client.app) is False
    assert not client.app.state.logs.verbose and logger.level == logging.INFO
    client.post("/system/console/verbose", data={"verbose": "on"})
    client.post("/system/console/verbose", data={})
    assert not client.app.state.logs.verbose
