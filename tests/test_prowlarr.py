import logging
from datetime import UTC, datetime

import pytest

from periodicool.prowlarr import (
    ProwlarrClient,
    ProwlarrError,
    RateLimited,
    parse_newznab,
    strip_apikey,
)

from fake_prowlarr import KEY, URL, FakeProwlarr, release_json


def client(fake, **kw):
    kw.setdefault("min_interval", 0)
    return ProwlarrClient(URL, KEY, transport=fake, **kw)


def test_search_sends_key_in_header_and_strips_it_from_urls():
    fake = FakeProwlarr()
    fake.search_results = [release_json("Fortean.Times.Issue.437-November.2023")]
    [rel] = client(fake).search("Fortean Times", [7010], [1, 2])
    call = fake.calls[0]
    assert call["headers"]["X-Api-Key"] == KEY
    assert KEY not in call["url"]
    assert call["query"]["indexerIds"] == ["1", "2"]
    assert call["query"]["categories"] == ["7010"]
    assert KEY not in rel.download_url and "link=abc" in rel.download_url
    assert rel.publish_date == datetime(2026, 10, 10, tzinfo=UTC)
    assert rel.categories == (7010,)


def test_rss_parses_newznab_and_apilimits():
    fake = FakeProwlarr()
    fake.rss_titles[1] = ["Linux Magazine USA - Issue 311, October 2026"]
    found, limits = client(fake).rss(1, [7010])
    assert fake.calls[0]["path"] == "/1/api"
    assert fake.calls[0]["query"]["t"] == ["search"]
    assert found[0].size == 52428800 and found[0].categories == (7010,)
    assert KEY not in found[0].download_url
    assert (limits.api_current, limits.api_max, limits.grab_max) == (12, 100, 10)


def test_newznab_error_document():
    with pytest.raises(ProwlarrError, match="100: Incorrect user credentials"):
        parse_newznab(b'<error code="100" description="Incorrect user credentials"/>')


def test_rate_limit_raises_and_errors_are_redacted(caplog):
    fake = FakeProwlarr()
    fake.fail_with = (429, b"slow down")
    with pytest.raises(RateLimited):
        client(fake).search("x", [7010])
    fake.fail_with = (500, f"boom apikey={KEY}".encode())
    with caplog.at_level(logging.DEBUG), pytest.raises(ProwlarrError) as exc:
        client(fake).rss(1, [7010])
    assert KEY not in str(exc.value)
    assert KEY not in caplog.text


def test_unreachable_is_redacted():
    def broken(url, headers, timeout):
        raise OSError(f"connection refused for {url}")

    with pytest.raises(ProwlarrError) as exc:
        ProwlarrClient(URL, KEY, transport=broken, min_interval=0).rss(1, [7010])
    assert KEY not in str(exc.value)


def test_calls_are_spaced():
    fake = FakeProwlarr()
    t = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    c = client(fake, min_interval=2.0, clock=lambda: t[0], sleep=sleep)
    c.indexers()
    t[0] += 0.5
    c.indexers()
    assert slept == [pytest.approx(1.5)]


def test_call_hook_never_sees_the_key():
    fake = FakeProwlarr()
    seen = []
    fake.fail_with = (500, f"oops {KEY}".encode())
    c = client(fake, on_call=lambda **kw: seen.append(kw))
    with pytest.raises(ProwlarrError):
        c.indexers()
    assert seen and all(KEY not in str(v) for kw in seen for v in kw.values())


def test_indexers_and_stats():
    fake = FakeProwlarr()
    fake.stats = {"indexers": [{"indexerId": 1, "numberOfQueries": 5, "numberOfRssQueries": 7,
                                "numberOfGrabs": 2}]}  # fmt: skip
    c = client(fake)
    infos = {i.id: i for i in c.indexers()}
    assert infos[1].query_limit == 100 and infos[1].supports_category
    assert infos[3].protocol == "torrent"
    stats = c.indexer_stats(datetime(2026, 10, 16, tzinfo=UTC))
    assert stats[1] == {"queries": 5, "rss": 7, "grabs": 2}
    assert fake.calls[-1]["query"]["startDate"] == ["2026-10-16T00:00:00Z"]
    assert "endDate" not in fake.calls[-1]["query"]


def test_strip_apikey():
    assert strip_apikey("http://x/1/download?apikey=abc&link=z") == "http://x/1/download?link=z"
    assert strip_apikey(None) is None


# Redirects and readable errors


CLOUDFLARE = (
    b'{"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/'
    b'cloudflare-1xxx-errors/error-1010/","title":"Error 1010: Access denied","status":403,'
    b'"detail":"The site owner has blocked access based on your browser\'s signature."}'
)


def test_nzb_redirect_followed_once_without_the_key():
    fake = FakeProwlarr()
    fake.download_redirect = "https://indexer.test/getnzb?id=abc&r=indexerkey"
    fake.indexer_reply = (200, fake.nzb)
    client = ProwlarrClient(URL, KEY, transport=fake, min_interval=0)
    assert b"<nzb" in client.fetch_nzb(f"{URL}/1/download?link=abc")
    first, second = fake.calls[-2:]
    assert first["query"]["apikey"] == [KEY]
    assert second["url"].startswith("https://indexer.test/")
    assert KEY not in second["url"] and "X-Api-Key" not in second["headers"]


def test_cloudflare_refusal_reads_as_a_sentence():
    fake = FakeProwlarr()
    fake.download_redirect = "https://indexer.test/getnzb?id=abc"
    fake.indexer_reply = (403, CLOUDFLARE)
    client = ProwlarrClient(URL, KEY, transport=fake, min_interval=0)
    with pytest.raises(ProwlarrError) as err:
        client.fetch_nzb(f"{URL}/1/download?link=abc")
    message = str(err.value)
    assert message.startswith("the indexer (indexer.test) returned 403 for grab")
    assert "Error 1010: Access denied" in message and "Cloudflare" in message
    assert "b'" not in message


def test_only_nzb_downloads_follow_redirects():
    fake = FakeProwlarr()
    calls = []

    def redirecting(url, headers, timeout):
        calls.append(url)
        return 302, b"", {"Location": "https://elsewhere.test/"}

    client = ProwlarrClient(URL, KEY, transport=redirecting, min_interval=0)
    with pytest.raises(ProwlarrError, match="Prowlarr returned 302"):
        client.indexers()
    assert len(calls) == 1
    assert fake.calls == []


def test_real_transport_sends_our_user_agent_and_never_follows(monkeypatch):
    import urllib.error
    import urllib.request

    from periodicool import USER_AGENT
    from periodicool import prowlarr as module

    seen = {}

    def fake_open(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        raise urllib.error.HTTPError(
            req.full_url, 302, "Found", {"Location": "https://x.test/"}, None
        )

    monkeypatch.setattr(module._OPENER, "open", fake_open)
    status, body, headers = module._urllib_transport(f"{URL}/api/v1/indexer", {}, 5)
    assert seen["ua"] == USER_AGENT and status == 302 and headers["Location"] == "https://x.test/"
    assert any(isinstance(h, module._NoRedirect) for h in module._OPENER.handlers)
    assert urllib.request  # the opener replaces urllib's redirect handler
