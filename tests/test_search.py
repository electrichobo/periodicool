"""Decisions, RSS sync, magazine search, Wanted search and jobs. No network."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import db, library, search
from periodicool.cli import magazine_from_dict
from periodicool.config import load_settings
from periodicool.decisions import best_candidates, evaluate, format_rank
from periodicool.jobs import Scheduler, today_in
from periodicool.models import (
    ApiCall,
    FormatProfile,
    Indexer,
    Issue,
    IssueStatus,
    Job,
    Magazine,
    MonitorMode,
    Release,
    SearchState,
)
from periodicool.prowlarr import ProwlarrClient
from rows import add_indexer

from fake_prowlarr import KEY, URL, FakeProwlarr, release_json

EXAMPLES = Path(__file__).parent.parent / "examples" / "magazines.json"
TODAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def session(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = db.make_engine(url)
    with db.make_session_factory(engine)() as s:
        profile = s.scalars(select(FormatProfile)).one()
        for entry in json.loads(EXAMPLES.read_text(encoding="utf-8")):
            mag = magazine_from_dict(entry)
            mag.format_profile = profile
            s.add(mag)
        add_indexer(s, URL)
        s.flush()
        yield s
    engine.dispose()


@pytest.fixture
def fake():
    return FakeProwlarr()


@pytest.fixture
def client(session, fake):
    c = search.make_client(session, transport=fake, api_key=KEY)
    c.min_interval = 0
    return c


def mag(session, title) -> Magazine:
    return session.scalars(select(Magazine).where(Magazine.title == title)).one()


def ev(session, title, **kw):
    mags = list(session.scalars(select(Magazine)))
    kw.setdefault("size", 50 * 1048576)
    return evaluate(session, title, mags, TODAY, **kw)


# Decisions


def test_accepts_current_issue(session):
    e = ev(session, "Linux Magazine USA - Issue 311, October 2026")
    assert e.accepted and e.issue_key == "n311" and e.reasons == []


@pytest.mark.parametrize(
    ("title", "kw", "reason"),
    [
        ("Linux Magazine USA - Issue 311, October 2026", {"protocol": "torrent"}, "protocol"),
        ("Linux Magazine USA - Issue 311, October 2026", {"size": 1000}, "under 1 MB"),
        ("People.Australia.TruePDF-08.April.2019", {}, "region Australia, wanted USA"),
        ("Linux Magazine #03 (2026)", {}, "does not fit 2026"),
        ("MAKE.Magazine.01-45.Mega.Pack.-.DIY.Projects.and.Ideas.for.Makers.TruePDF", {}, "pack"),
        ("WineMaker.April-May.2026.HYBRID.MAGAZINE", {}, "no tracked magazine"),
        ("American.Cinematographer-September.2023", {}, "before the monitored range"),
        (
            "Fortean.Times.No.416.2022.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE",
            {},
            "not newer than issue 437",
        ),
        ("Fortean.Times.TruePDF-October", {}, "month without year"),
        ("Linux Magazine USA - Issue 314, January 2027", {}, "more than 45 days away"),
    ],
)
def test_rejections(session, title, kw, reason):
    e = ev(session, title, **kw)
    assert not e.accepted
    assert any(reason in r for r in e.reasons), e.reasons


def test_all_reasons_are_collected(session):
    e = ev(session, "Linux Magazine USA - Issue 311, October 2026", protocol="torrent", size=10)
    assert len(e.reasons) >= 2


def test_release_rules(session):
    linux = mag(session, "Linux Magazine")
    linux.must_not_contain = ["usa"]
    assert any(
        "rejected term" in r
        for r in ev(session, "Linux Magazine USA - Issue 311, October 2026").reasons
    )
    linux.must_not_contain, linux.must_contain = [], ["hybrid"]
    assert any(
        "required term" in r
        for r in ev(session, "Linux Magazine USA - Issue 311, October 2026").reasons
    )


def test_global_release_rules(session):
    from periodicool.decisions import MUST_CONTAIN_KEY, MUST_NOT_CONTAIN_KEY, load_options

    title = "Linux Magazine USA - Issue 311, October 2026"
    library.set_setting(session, MUST_NOT_CONTAIN_KEY, ["USA"])
    e = ev(session, title, options=load_options(session))
    assert not e.accepted and any("(all magazines)" in r for r in e.reasons)
    library.set_setting(session, MUST_NOT_CONTAIN_KEY, [])
    library.set_setting(session, MUST_CONTAIN_KEY, ["issue"])
    assert ev(session, title, options=load_options(session)).accepted


def test_issue_state_matters(session):
    linux = mag(session, "Linux Magazine")
    issue = Issue(magazine_id=linux.id, key="n311", cover_date=date(2026, 10, 1), issue_number=311)
    session.add(issue)
    session.flush()
    assert ev(session, "Linux Magazine USA - Issue 311, October 2026").accepted
    issue.status = IssueStatus.GRABBED
    assert (
        "already grabbed" in ev(session, "Linux Magazine USA - Issue 311, October 2026").reasons[0]
    )
    issue.status, issue.file_path = IssueStatus.IMPORTED, "x.pdf"
    assert "already have" in ev(session, "Linux Magazine USA - Issue 311, October 2026").reasons[0]
    issue.file_path, issue.status, issue.monitored = None, IssueStatus.WANTED, False
    assert "not monitored" in ev(session, "Linux Magazine USA - Issue 311, October 2026").reasons[0]


def test_monitor_modes_for_unknown_issues(session):
    ac = mag(session, "American Cinematographer")
    ac.monitor_mode, ac.monitor_from_year = MonitorMode.FROM_YEAR, 2023
    assert ev(session, "American.Cinematographer-September.2023").accepted
    ac.monitor_mode = MonitorMode.ALL
    assert ev(session, "American.Cinematographer.TruePDF-September.2007").accepted
    make = mag(session, "Make:")
    recent = datetime(2026, 9, 30, tzinfo=UTC)
    assert ev(session, "Make.No.98.2026.RETAIL.MAGAZINE.eBook-21A1", published=recent).accepted
    old = datetime(2025, 1, 1, tzinfo=UTC)
    assert not ev(session, "Make.No.98.2026.RETAIL.MAGAZINE.eBook-21A1", published=old).accepted
    assert make.monitor_mode is MonitorMode.NEW_ONLY


def test_format_profile(session):
    profile = session.scalars(select(FormatProfile)).one()
    profile.formats = ["pdf"]
    e = ev(session, "Linux Magazine USA - Issue 311, October 2026 cbz")
    assert any("not in profile" in r for r in e.reasons)
    assert format_rank("pdf", True, profile) < format_rank("pdf", False, profile)
    assert format_rank("pdf", False, None) < format_rank("cbz", True, None)


# RSS sync


def test_rss_sync_records_and_dedupes(session, client, fake):
    fake.rss_titles[1] = [
        "Linux Magazine USA - Issue 311, October 2026",
        "WineMaker.April-May.2026.HYBRID.MAGAZINE",
    ]
    fake.rss_titles[2] = ["Linux.Magazine.No.311.2026.HYBRID.MAGAZINE"]
    r = search.rss_sync(session, client, TODAY, NOW)
    assert (r.seen, r.new, len(r.accepted)) == (3, 3, 2)
    assert {p["path"] for p in fake.feeds()} == {"/1/api", "/2/api"}  # not the torrent one
    stored = session.scalars(select(Release)).all()
    assert all(KEY not in (x.download_url or "") for x in stored)
    assert {x.indexer for x in stored} == {"Alpha", "Beta"}

    r = search.rss_sync(session, client, TODAY, NOW)
    assert r.new == 0 and len(session.scalars(select(Release)).all()) == 3
    best = best_candidates(session)
    assert [(b.issue_key, b.accepted) for b in best] == [("n311", True)]


def test_rss_skips_disabled_and_limited_indexers(session, client, fake):
    fake.status = [{"indexerId": 2, "disabledTill": "2026-10-16T18:00:00Z"}]
    fake.stats = {
        "indexers": [
            {"indexerId": 1, "numberOfQueries": 90, "numberOfRssQueries": 10, "numberOfGrabs": 0}
        ]
    }
    r = search.rss_sync(session, client, TODAY, NOW)
    assert fake.feeds() == []
    assert "query limit reached" in r.skipped["Alpha"] and "disabled" in r.skipped["Beta"]


def test_rate_limit_backs_off_per_indexer_entry(session, client, fake):
    fake.rss_titles[1] = ["x"]
    calls_before = len(fake.calls)
    fake.fail_with = None

    def throttle(url, headers, timeout):
        if url.startswith(f"{URL}/1/api"):
            fake.calls.append({"url": url, "path": "/1/api"})
            return 429, b""
        return FakeProwlarr.__call__(fake, url, headers, timeout)

    client._transport = throttle
    r = search.rss_sync(session, client, TODAY, NOW)
    assert "rate limited" in r.errors[0]
    assert search._aware(client.row.backoff_until) == NOW + timedelta(minutes=5)
    calls_after = len(fake.calls)
    assert calls_after > calls_before
    r = search.rss_sync(session, client, TODAY, NOW + timedelta(minutes=1))
    assert "slow down" in r.skipped["Prowlarr"] and len(fake.calls) == calls_after
    # Next rate limit doubles the wait.
    search._rate_limited(session, NOW, client)
    assert client.row.backoff_s == 600


# Magazine search


def test_search_covers_many_issues_and_uses_cache(session, client, fake):
    linux = mag(session, "Linux Magazine")
    fake.search_results = [
        release_json("Linux Magazine USA - Issue 311, October 2026", indexer_id=1),
        release_json("Linux.Magazine.No.310.2026.HYBRID.MAGAZINE", indexer_id=2),
        release_json("Linux Magazine #03 (2026)", indexer_id=2),
    ]
    r = search.search_magazine(session, client, linux, TODAY, now=NOW)
    assert not r.from_cache and len(r.candidates) == 3 and r.new_accepted == 1
    sent = fake.paths("/api/v1/search")[0]["query"]
    assert sent["query"] == ["Linux Magazine"] and sent["indexerIds"] == ["1", "2"]

    again = search.search_magazine(session, client, linux, TODAY, now=NOW + timedelta(hours=1))
    assert again.from_cache and len(fake.paths("/api/v1/search")) == 1
    search.search_magazine(session, client, linux, TODAY, force=True, now=NOW)
    assert len(fake.paths("/api/v1/search")) == 2


def test_search_term_and_aliases(session, client, fake):
    make = mag(session, "Make:")
    make.aliases = ["MAKE Volume"]
    search.search_magazine(session, client, make, TODAY, now=NOW)
    queries = [c["query"]["query"][0] for c in fake.paths("/api/v1/search")]
    assert queries == ["Make Magazine", "MAKE Volume"]
    assert search.default_search_term(mag(session, "2600: The Hacker Quarterly")) == (
        "2600 The Hacker Quarterly"
    )


def test_optional_daily_cap(session, client, fake):
    library.set_setting(session, search.DAILY_CAP_KEY, 1)
    session.add(ApiCall(at=NOW, kind="search", ok=True))
    session.flush()
    with pytest.raises(search.Unavailable, match="daily search cap"):
        search.search_magazine(session, client, mag(session, "People"), TODAY, now=NOW)
    assert fake.paths("/api/v1/search") == []


def test_no_usable_indexer(session, client, fake):
    fake.status = [
        {"indexerId": 1, "disabledTill": "2026-10-17T00:00:00Z"},
        {"indexerId": 2, "disabledTill": "2026-10-17T00:00:00Z"},
    ]
    with pytest.raises(search.Unavailable, match="no indexer available"):
        search.search_magazine(session, client, mag(session, "People"), TODAY, now=NOW)


def test_search_backoff_doubles_and_resets(session, client, fake):
    linux = mag(session, "Linux Magazine")
    waits = []
    t = NOW
    for _ in range(7):
        search.search_magazine(session, client, linux, TODAY, scheduled=True, force=True, now=t)
        state = session.get(SearchState, linux.id)
        waits.append(state.next_search_at.replace(tzinfo=UTC) - t)
    assert [w.total_seconds() / 3600 for w in waits] == [6, 12, 24, 48, 96, 168, 168]

    fake.search_results = [release_json("Linux Magazine USA - Issue 311, October 2026")]
    search.search_magazine(session, client, linux, TODAY, scheduled=True, force=True, now=t)
    state = session.get(SearchState, linux.id)
    assert state.attempts == 0 and "1 new" in state.last_result


def test_wanted_search_is_bounded_and_never_repeats(session, client, fake):
    results = search.wanted_search(session, client, TODAY, max_magazines=3, now=NOW)
    assert len(results) == 3
    searched = [c["query"]["query"][0] for c in fake.paths("/api/v1/search")]
    assert len(searched) == len(set(searched)) == 3
    # The same magazines are not due again until their backoff passes.
    more = search.wanted_search(session, client, TODAY, max_magazines=20, now=NOW)
    titles = {r.magazine.title for r in results} & {r.magazine.title for r in more}
    assert titles == set()
    later = search.wanted_search(
        session, client, TODAY, max_magazines=20, now=NOW + timedelta(days=8)
    )
    assert len(later) >= 3


# Jobs


def test_scheduler_runs_due_jobs_once(tmp_path):
    settings = load_settings(
        {
            "PERIODICOOL_CONFIG_DIR": str(tmp_path / "config"),
            "PERIODICOOL_LIBRARY_DIR": str(tmp_path / "library"),
        }
    )
    settings.config_dir.mkdir()
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    sessions = db.make_session_factory(engine)
    clock = [NOW]
    ran = []

    def boom(session, settings, now):
        raise RuntimeError("broken job")

    runners = {
        "rss": lambda s, st, now: ran.append("rss") or "ok",
        "wanted": boom,
        "library": search_unconfigured,
        "import": lambda s, st, now: "skipped",
    }
    sched = Scheduler(sessions, settings, clock=lambda: clock[0], runners=runners)
    first = sched.run_due()
    assert first["rss"] == "ok" and first["wanted"].startswith("error: broken job")
    assert sched.run_due() == {}  # nothing due a second later
    clock[0] = NOW + timedelta(minutes=16)
    assert set(sched.run_due()) == {"rss", "import"}  # import runs every 5 minutes
    with sessions() as s:
        job = s.get(Job, "rss")
        assert job.interval_s == 900 and "ok" in job.last_result
    engine.dispose()


def search_unconfigured(session, settings, now):
    from periodicool.jobs import run_rss

    return run_rss(session, settings, now)


def test_real_runners_skip_when_unconfigured(tmp_path):
    from periodicool.jobs import run_import, run_library, run_rss, run_wanted

    settings = load_settings(
        {
            "PERIODICOOL_CONFIG_DIR": str(tmp_path / "c"),
            "PERIODICOOL_LIBRARY_DIR": str(tmp_path / "missing"),
        }
    )
    settings.config_dir.mkdir()
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    with db.make_session_factory(engine)() as s:
        assert run_rss(s, settings, NOW).startswith("skipped")
        assert run_wanted(s, settings, NOW).startswith("skipped")
        assert run_library(s, settings, NOW).startswith("skipped")
        assert run_import(s, settings, NOW).startswith("skipped")
    engine.dispose()


def test_today_in_time_zone():
    late = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)  # 20:00 the day before in Los Angeles
    assert today_in("America/Los_Angeles", late) == date(2026, 10, 16)
    assert today_in("UTC", late) == date(2026, 10, 17)


def test_jobs_switch():
    assert load_settings({"PERIODICOOL_JOBS": "off"}).jobs_enabled is False
    assert load_settings({}).jobs_enabled is True


def test_client_needs_url_and_key(session, monkeypatch):
    monkeypatch.delenv("PROWLARR_API_KEY", raising=False)
    assert search.make_client(session) is None
    assert search.make_client(session, api_key=KEY) is not None
    session.scalars(select(Indexer)).one().url = ""
    assert search.make_client(session, api_key=KEY) is None


def test_rate_limited_search_raises(session, client, fake):
    fake.fail_with = None
    calls = []

    def throttle(url, headers, timeout):
        calls.append(url)
        if "/api/v1/search" in url:
            return 429, b""
        return FakeProwlarr.__call__(fake, url, headers, timeout)

    client._transport = throttle
    with pytest.raises(search.Unavailable, match="rate limited"):
        search.search_magazine(session, client, mag(session, "People"), TODAY, now=NOW)
    assert client.row.backoff_until
    assert isinstance(client, ProwlarrClient)


# Fortean Times: number-only names get an estimated month


FT_475 = "Fortean.Times.No.475.2026.RETAIL.MAGAZINE.eBook-21A1"


def test_number_only_name_gets_estimated_month(session):
    e = ev(session, FT_475)
    assert e.accepted and e.issue_key == "2026-10"
    assert e.fields["issue_number"] == 475
    assert e.notes == ["month 2026-10 estimated from issue 475 (13 a year from 437)"]


def test_estimate_is_flagged_when_stored(session, client, fake):
    fake.search_results = [release_json(FT_475)]
    r = search.search_magazine(session, client, mag(session, "Fortean Times"), TODAY, now=NOW)
    [(rel, _)] = r.candidates
    assert rel.accepted and rel.reasons == [
        "note: month 2026-10 estimated from issue 475 (13 a year from 437)"
    ]


def test_estimate_uses_newest_dated_issue(session):
    ft = mag(session, "Fortean Times")
    session.add(
        Issue(magazine_id=ft.id, key="2026-08", cover_date=date(2026, 8, 1), issue_number=473)
    )
    session.flush()
    e = ev(session, FT_475)
    assert e.issue_key == "2026-10" and "from 473" in e.notes[0]


def test_estimate_only_for_newer_numbers(session):
    ft = mag(session, "Fortean Times")
    session.add(
        Issue(magazine_id=ft.id, key="2026-11", cover_date=date(2026, 11, 1), issue_number=476)
    )
    session.flush()
    e = ev(session, FT_475)
    assert not e.accepted and "not newer than issue 476" in e.reasons[0]


def test_estimate_rejects_month_holding_another_issue(session):
    ft = mag(session, "Fortean Times")
    session.add(Issue(magazine_id=ft.id, key="2026-10", issue_number=470))
    session.flush()
    e = ev(session, FT_475)
    assert not e.accepted and "already holds issue 470" in " ".join(e.reasons)


def test_estimate_must_match_the_year_in_the_name(session):
    e = ev(session, "Fortean.Times.No.480.2025.RETAIL.MAGAZINE.eBook-21A1")
    assert not e.accepted and "is not in 2025" in e.reasons[0]


def test_library_never_estimates(session, tmp_path):
    from periodicool.models import ReasonCode, ScanKind

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / f"{FT_475}.pdf").write_bytes(b"x")
    scan = library.plan_scan(session, lib, ScanKind.LIBRARY)
    [item] = library.queue(session)
    assert item.reason_code is ReasonCode.UNIDENTIFIED and "number without a month" in item.reason
    assert scan.counts["move"] == 0
