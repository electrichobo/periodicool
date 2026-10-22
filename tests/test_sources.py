"""Indexer and download client lists: building clients, direct Newznab indexers,
searching several entries, and grabbing through several download clients. No network."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from fake_newznab import NZ_KEY, NZ_URL, FakeNewznab
from periodicool import downloads, search
from periodicool.logs import Redactor, stored_secrets
from periodicool.models import ApiCall, DownloadClient, Grab, Indexer, Magazine, Release
from periodicool.newznab import NewznabClient
from periodicool.prowlarr import ProwlarrError
from periodicool.sources import PROWLARR_KEY_ENV, SAB_KEY_ENV, Sources
from rows import add_client, add_indexer

from fake_prowlarr import KEY, release_json
from fake_sab import SAB_KEY, SAB_URL
from test_downloads import L311, L311_ALT, TODAY, env  # noqa: F401

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
ENV = {PROWLARR_KEY_ENV: KEY, SAB_KEY_ENV: SAB_KEY}


@pytest.fixture
def fake_n():
    return FakeNewznab()


def sources(e, fake_n, env=ENV) -> Sources:
    src = Sources(e.session, {"prowlarr": e.fake_p, "newznab": fake_n, "sab": e.fake_s}, env)
    for c in src.indexers + src.clients:
        c.min_interval = 0
    return src


def with_newznab(e, priority=10, **settings):
    return add_indexer(
        e.session,
        NZ_URL + "/api",
        NZ_KEY,
        "Direct",
        "newznab",
        priority=priority,
        settings=settings,
    )


def linux(e) -> Magazine:
    return e.session.scalars(select(Magazine).where(Magazine.title == "Linux Magazine")).one()


# Building clients


def test_order_disabled_and_env_fallback(env, fake_n):
    e = env
    nz = with_newznab(e)
    second = add_indexer(e.session, "http://other:9696", name="Second Prowlarr", priority=1)
    off = add_indexer(e.session, "http://off:9696", "k" * 8, "Off", enabled=False)
    src = sources(e, fake_n)
    # The second Prowlarr has no key, and the environment only covers the first one.
    assert [c.source_name for c in src.indexers] == ["Direct", "Prowlarr"]
    assert isinstance(src.indexers[0], NewznabClient)
    assert second.id and off.id
    assert src.first_prowlarr().source_name == "Prowlarr"
    assert nz.id == src.indexers[0].source_id


def test_client_for_protocol_and_priority(env, fake_n):
    e = env
    add_client(e.session, "http://sab2:8080", SAB_KEY, "Backup", priority=5)
    src = sources(e, fake_n)
    assert src.client_for("usenet").display_name == "SABnzbd"
    assert src.client_for("torrent") is None
    e.client_row.enabled = False
    src = sources(e, fake_n)
    assert src.client_for("usenet").display_name == "Backup"


# Direct Newznab


def test_newznab_caps_search_and_safety(env, fake_n):
    e = env
    with_newznab(e)
    client = sources(e, fake_n).indexers[0]
    caps = client.caps()
    assert caps["title"] == "Test Indexer" and caps["search"] and caps["magazines"]
    fake_n.search_titles = [L311]
    [found] = client.search("Linux Magazine", [7010])
    assert found.indexer == "Direct" and NZ_KEY not in (found.download_url or "")
    assert fake_n.calls[-1]["path"] == "/api" and fake_n.calls[-1]["query"]["q"] == "Linux Magazine"
    calls = e.session.scalars(select(ApiCall).where(ApiCall.source_id == client.source_id)).all()
    assert {c.kind for c in calls} == {"caps", "search"}
    with pytest.raises(ProwlarrError, match="not on the configured"):
        client.fetch_nzb("https://elsewhere.test/getnzb/1.nzb")


# Searching several entries


def test_search_merges_entries_with_a_cache_each(env, fake_n):
    e = env
    with_newznab(e)
    e.fake_p.search_results = [release_json(L311)]
    fake_n.search_titles = [L311_ALT]
    src = sources(e, fake_n)
    result = search.search_magazine(e.session, src, linux(e), TODAY, now=NOW)
    titles = {rel.title: rel for rel, _ in result.candidates}
    assert set(titles) >= {L311, L311_ALT}
    ids = {row.name: row.id for row in e.session.scalars(select(Indexer))}
    assert titles[L311].source_id == ids["Prowlarr"]
    assert titles[L311_ALT].source_id == ids["Direct"]
    before = len(fake_n.calls), len(e.fake_p.calls)
    again = search.search_magazine(e.session, src, linux(e), TODAY, now=NOW)
    assert again.from_cache and (len(fake_n.calls), len(e.fake_p.calls)) == before


def test_one_entry_rate_limited_others_still_search(env, fake_n):
    e = env
    nz = with_newznab(e)
    e.fake_p.search_results = [release_json(L311)]
    fake_n.fail_with = (429, b"")
    result = search.search_magazine(e.session, sources(e, fake_n), linux(e), TODAY, now=NOW)
    assert L311 in {rel.title for rel, _ in result.candidates}
    assert search._aware(nz.backoff_until) == NOW + timedelta(minutes=5)
    assert e.session.scalars(select(Indexer).where(Indexer.name == "Prowlarr")).one().backoff_s == 0
    # While it backs off, the entry is skipped and not called.
    calls = len(fake_n.calls)
    later = search.search_magazine(
        e.session, sources(e, fake_n), linux(e), TODAY, force=True, now=NOW + timedelta(minutes=1)
    )
    assert "slow down" in later.skipped["Direct"] and len(fake_n.calls) == calls


def test_newznab_daily_cap(env, fake_n):
    e = env
    nz = with_newznab(e, daily_cap=1)
    e.session.add(ApiCall(at=NOW, kind="search", source_id=nz.id, ok=True, status=200))
    e.session.flush()
    [lim] = search.indexer_limits(e.session, sources(e, fake_n).indexers[0], NOW)
    assert lim.query_limit == 1 and lim.queries_left == 0
    e.fake_p.search_results = [release_json(L311)]
    calls = len(fake_n.calls)
    result = search.search_magazine(e.session, sources(e, fake_n), linux(e), TODAY, now=NOW)
    assert len(fake_n.calls) == calls and L311 in {rel.title for rel, _ in result.candidates}


def test_rss_from_every_entry(env, fake_n):
    e = env
    with_newznab(e)
    e.fake_p.rss_titles[1] = [L311]
    fake_n.rss_titles = [L311_ALT]
    r = search.rss_sync(e.session, sources(e, fake_n), TODAY, NOW)
    assert {rel.title for rel in r.accepted} >= {L311, L311_ALT}
    assert "q" not in fake_n.calls[-1]["query"]


def test_no_entry_could_search(env, fake_n):
    e = env
    e.fake_p.fail_with = (500, b"broken")
    with pytest.raises(search.Unavailable):
        search.search_magazine(e.session, sources(e, fake_n), linux(e), TODAY, now=NOW)


# Grabbing through several download clients


def test_grab_goes_to_the_first_client_and_records_it(env, fake_n):
    e = env
    backup = add_client(e.session, "http://sab2:8080", SAB_KEY, "Backup", "mags2", priority=5)
    with_newznab(e)
    fake_n.search_titles = [L311]
    src = sources(e, fake_n)
    search.search_magazine(e.session, src, linux(e), TODAY, now=NOW)
    rel = e.session.scalars(select(Release).where(Release.title == L311)).one()
    res = downloads.grab(e.session, rel, src, src, TODAY)
    assert res.ok and res.grab.client_id == e.client_row.id and res.client_name == "SABnzbd"
    assert e.fake_s.added[0][0] == "mags"
    assert fake_n.calls[-1]["path"].startswith("/getnzb/")  # NZB from the release's own entry
    # Monitor asks each client; Remove goes to the grab's own client.
    downloads.monitor(e.session, src, e.settings, src, TODAY)
    hosts = {c["url"].split("/")[2] for c in e.fake_s.calls if c["query"].get("mode") == "queue"}
    assert hosts == {SAB_URL.split("/")[2]}  # the backup client has no jobs, so is not asked
    downloads.remove_grab(e.session, res.grab.id, src)
    removes = [c for c in e.fake_s.calls if c["query"].get("name") == "delete"]
    assert removes and removes[-1]["url"].startswith(SAB_URL)
    assert backup.id


def test_grab_refused_without_a_client_for_the_protocol(env, fake_n):
    e = env
    e.client_row.enabled = False
    e.fake_p.search_results = [release_json(L311)]
    src = sources(e, fake_n)
    search.search_magazine(e.session, src, linux(e), TODAY, now=NOW)
    rel = e.session.scalars(select(Release).where(Release.title == L311)).one()
    res = downloads.grab(e.session, rel, src, src, TODAY)
    assert not res.ok and "no enabled usenet download client" in res.reasons


def test_remove_refused_when_the_client_is_gone(env, fake_n):
    e = env
    e.fake_p.search_results = [release_json(L311)]
    src = sources(e, fake_n)
    search.search_magazine(e.session, src, linux(e), TODAY, now=NOW)
    rel = e.session.scalars(select(Release).where(Release.title == L311)).one()
    grab = downloads.grab(e.session, rel, src, src, TODAY).grab
    e.client_row.enabled = False
    with pytest.raises(ValueError, match="removed or disabled"):
        downloads.remove_grab(e.session, grab.id, sources(e, fake_n))
    assert e.session.get(Grab, grab.id).status.value != "removed"


# Redaction


def test_stored_keys_are_redacted(env):
    e = env
    with_newznab(e)
    e.session.get(DownloadClient, e.client_row.id).password = "client-password-1"
    redactor = Redactor([])
    redactor.learn(stored_secrets(e.session))
    text = redactor.redact(f"failed with {NZ_KEY} and client-password-1")
    assert NZ_KEY not in text and "client-password-1" not in text
