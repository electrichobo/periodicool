"""Activity, Wanted, Calendar and the iCal feed over HTTP. Fakes only."""

from datetime import timedelta

from sqlalchemy import select

from periodicool import auth, history
from periodicool.jobs import today_in
from periodicool.models import (
    BlockedRelease,
    DownloadClient,
    FormatProfile,
    Grab,
    GrabStatus,
    HistoryEvent,
    Issue,
    IssueStatus,
    Magazine,
)

from test_web_magazines import fakes, mags, session, toast  # noqa: F401


def _grab(client, mag_id, status=GrabStatus.DOWNLOADING, job="SABnzbd_nzo_1", key="n311"):
    with session(client) as s:
        g = Grab(
            magazine_id=mag_id,
            issue_key=key,
            title="Linux.Magazine.311.<b>bold</b>",
            client="sabnzbd",
            client_id=s.scalar(select(DownloadClient.id)),
            job_id=job,
            category="mags",
            status=status,
            progress=42.0,
            size_bytes=80 * 1024 * 1024,
            size_left_bytes=40 * 1024 * 1024,
            time_left_s=95,
        )
        s.add(g)
        s.commit()
        return g.id


# Queue


def test_activity_redirects(client):
    assert client.get("/activity", follow_redirects=False).headers["location"] == "/activity/queue"
    assert client.get("/history", follow_redirects=False).headers["location"] == "/activity/history"
    assert client.get("/wanted", follow_redirects=False).headers["location"] == "/wanted/missing"


def test_queue_lists_our_grabs_and_polls_only_while_active(client, mags):
    empty = client.get("/activity/queue/table").text
    assert "Queue is empty" in empty and "every 5s" not in empty
    _grab(client, mags["Linux Magazine"])
    page = client.get("/activity/queue").text
    assert "Downloading" in page and "42%" in page and "1:35" in page and "80.0 MB" in page
    assert "&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page
    assert "every 5s" in client.get("/activity/queue/table").text


def test_queue_actions_need_sab(client, mags):
    grab_id = _grab(client, mags["Linux Magazine"])
    assert toast(client.post("/tasks/downloads"))["kind"] == "warn"
    assert "Add to blocklist" in client.get(f"/activity/queue/{grab_id}/remove").text
    assert toast(client.post(f"/activity/queue/{grab_id}/remove"))["kind"] == "warn"


def test_remove_from_queue(client, mags, fakes):
    _, fake_s = fakes
    from periodicool.clients.sabnzbd import SabnzbdClient

    from fake_sab import SAB_KEY, SAB_URL, make_nzb

    job = SabnzbdClient(SAB_URL, SAB_KEY, transport=fake_s, min_interval=0).add_nzb(
        make_nzb("a.pdf"), "Linux 311", "mags"
    )
    grab_id = _grab(client, mags["Linux Magazine"], job=job)
    resp = client.post(f"/activity/queue/{grab_id}/remove", data={"blocklist": "on"})
    trigger = toast(resp)
    assert trigger["kind"] == "ok" and fake_s.deleted == [("queue", job, "1")]
    with session(client) as s:
        assert s.get(Grab, grab_id).status is GrabStatus.REMOVED
        assert s.scalars(select(BlockedRelease)).one()
    # Removing again is refused, and SAB is not called a second time.
    assert toast(client.post(f"/activity/queue/{grab_id}/remove"))["kind"] == "bad"
    assert len(fake_s.deleted) == 1


# History


def test_history_page_filter_paging_and_details(client, mags):
    mag_id = mags["Fortean Times"]
    with session(client) as s:
        for n in range(55):
            history.record(
                s,
                HistoryEvent.FILE_MOVED,
                magazine_id=mag_id,
                issue_key=f"n{n}",
                source_title=f"file {n}",
            )
        history.record(s, HistoryEvent.BLOCKLISTED, magazine_id=mag_id, source_title="<i>x</i>")
        s.commit()
    page = client.get("/activity/history").text
    assert "56 events" in page and 'class="pager"' in page and "&lt;i&gt;x&lt;/i&gt;" in page
    second = client.get("/activity/history?page=2").text
    assert second.count("File moved") >= 6
    only = client.get("/activity/history?event=blocklisted").text
    assert "1 events" in only and "file 3" not in only
    assert "56 events" in client.get("/activity/history?event=bogus").text
    with session(client) as s:
        from periodicool.models import History

        h_id = s.scalars(select(History.id).where(History.event == HistoryEvent.FILE_MOVED)).first()
    detail = client.get(f"/history/{h_id}").text
    assert "File moved" in detail and "Mark as failed" not in detail


def test_mark_imported_grab_failed_keeps_file(client, settings, mags):
    grab_id = _grab(client, mags["Linux Magazine"], status=GrabStatus.IMPORTED)
    with session(client) as s:
        g = s.get(Grab, grab_id)
        h = history.record_grab(s, HistoryEvent.IMPORTED, g)
        s.commit()
        h_id = h.id
    assert "Mark as failed" in client.get(f"/history/{h_id}").text
    resp = client.post(f"/history/{h_id}/fail")
    assert "blocklisted" in toast(resp)["text"]
    with session(client) as s:
        assert s.get(Grab, grab_id).status is GrabStatus.FAILED
        assert s.scalars(select(BlockedRelease)).one().title.startswith("Linux.Magazine.311")
    assert toast(client.post(f"/history/{h_id}/fail"))["kind"] == "warn"


# Blocklist


def test_blocklist_remove_and_clear(client, mags):
    with session(client) as s:
        for n in range(3):
            s.add(BlockedRelease(title=f"bad {n}", reason="test", magazine_id=mags["People"]))
        s.commit()
        ids = list(s.scalars(select(BlockedRelease.id)))
    page = client.get("/activity/blocklist").text
    assert "3 blocklisted" in page and "bad 2" in page and "People" in page
    client.post("/activity/blocklist/remove", data={"ids": [str(ids[0])]})
    with session(client) as s:
        assert len(s.scalars(select(BlockedRelease)).all()) == 2
    assert toast(client.post("/activity/blocklist/remove"))["kind"] == "warn"
    client.post("/activity/blocklist/clear")
    with session(client) as s:
        assert s.scalars(select(BlockedRelease)).all() == []


# Wanted


def test_missing_lists_and_unmonitors(client, mags):
    page = client.get("/wanted/missing").text
    assert "Missing" in page and "Fortean Times" in page and "Never" in page
    with session(client) as s:
        ids = [
            str(i.id)
            for i in s.scalars(select(Issue).where(Issue.file_path.is_(None), Issue.monitored))
        ]
    assert ids
    assert toast(client.post("/wanted/search", data={"ids": ids}))["kind"] == "warn"  # no Prowlarr
    client.post("/wanted/unmonitor", data={"ids": ids})
    assert "Nothing missing" in client.get("/wanted/missing").text


def test_missing_skips_upcoming_and_unmonitored_magazines(client, mags):
    today = today_in(client.app.state.settings.tz)
    with session(client) as s:
        s.get(Magazine, mags["Fortean Times"]).monitored = False
        assert s.scalars(select(Issue).where(Issue.cover_date > today)).first()
        s.commit()
    page = client.get("/wanted/missing").text
    assert "Fortean Times" not in page and "Upcoming" not in page


def test_cutoff_unmet(client, mags):
    with session(client) as s:
        profile = s.scalars(select(FormatProfile)).first()
        mag = s.get(Magazine, mags["Linux Magazine"])
        mag.format_profile = profile
        issue = s.scalars(select(Issue).where(Issue.magazine_id == mag.id)).first()
        issue.file_path, issue.file_format, issue.status = "x/y.cbz", "cbz", IssueStatus.IMPORTED
        s.commit()
        best = profile.formats[0].upper()
    page = client.get("/wanted/cutoff").text
    assert "Linux Magazine" in page and "CBZ" in page and best in page


# Calendar and iCal


def test_calendar_views(client, mags):
    today = today_in(client.app.state.settings.tz)
    month = client.get("/calendar").text
    assert today.strftime("%B %Y") in month and "is-today" in month and "Fortean Times" in month
    assert 'aria-current="true">Month' in month
    week = client.get(f"/calendar?view=week&day={today.isoformat()}").text
    assert "cal--week" in week
    agenda = client.get(f"/calendar?view=agenda&day={(today - timedelta(days=40)).isoformat()}")
    assert agenda.status_code == 200 and "agenda-day" in agenda.text
    assert client.get("/calendar?view=bogus&day=nonsense").status_code == 200
    assert "feed/calendar.ics?apikey=" in month


def test_ical_feed_needs_the_api_key(client, anon_client, settings, mags):
    assert client.get("/feed/calendar.ics").status_code == 401
    assert client.get("/feed/calendar.ics?apikey=wrong").status_code == 401
    key = auth.api_key(settings.config_dir)
    # Works without a login session, for calendar apps.
    resp = anon_client.get(f"/feed/calendar.ics?apikey={key}")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/calendar")
    text = resp.text
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" in text and "DTSTART;VALUE=DATE:" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    by_header = anon_client.get("/feed/calendar.ics", headers={"X-Api-Key": key})
    assert by_header.status_code == 200


def test_ical_escapes_and_folds():
    from periodicool.activity import _fold, _ical_text

    assert _ical_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    long = "SUMMARY:" + "é" * 80
    folded = _fold(long)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == long


def test_times_show_in_the_configured_zone():
    from datetime import UTC, datetime

    from periodicool import web

    web.DISPLAY_TZ["name"] = "America/Los_Angeles"
    try:
        assert web.when(datetime(2026, 10, 21, 23, 11, tzinfo=UTC)) == "2026-10-21 16:11"
        assert web.when(datetime(2026, 10, 21, 23, 11)) == "2026-10-21 16:11"  # naive is UTC
        assert web.when(None) == ""
        web.DISPLAY_TZ["name"] = "Not/AZone"
        assert web.when(datetime(2026, 10, 21, 23, 11, tzinfo=UTC)) == "2026-10-21 23:11"
    finally:
        web.DISPLAY_TZ["name"] = "UTC"


def test_retry_import_button_and_route(client, mags):
    grab_id = _grab(client, mags["Linux Magazine"], status=GrabStatus.REVIEW)
    assert "Retry import" not in client.get("/activity/queue").text  # no finished files yet
    with session(client) as s:
        s.get(Grab, grab_id).storage_path = "/data/complete/mags/x"
        s.commit()
    assert "Retry import" in client.get("/activity/queue").text
    resp = client.post(f"/activity/queue/{grab_id}/import")
    assert toast(resp)["kind"] == "bad"  # the folder is not there in this test
    other = _grab(client, mags["Linux Magazine"], job="SABnzbd_nzo_2")
    assert "waiting in review" in toast(client.post(f"/activity/queue/{other}/import"))["text"]
