"""A fake direct Newznab indexer transport for tests. No network."""

import urllib.parse

from fake_prowlarr import FakeProwlarr

NZ_URL = "https://nzb.test"
NZ_KEY = "newznab-key-456789"

CAPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps><server title="Test Indexer"/>
<searching><search available="yes" supportedParams="q"/></searching>
<categories><category id="7000" name="Books"><subcat id="7010" name="Mags"/></category></categories>
</caps>"""


def items_xml(titles, apilimits=True, torznab=False) -> bytes:
    if torznab:
        # Torznab: a magnet link only (no download link), with seeders and peers.
        items = "".join(
            f"""<item><title>{t}</title><guid>tz-{t}</guid>
            <pubDate>Fri, 09 Oct 2026 10:00:00 +0000</pubDate>
            <torznab:attr name="category" value="7010"/>
            <torznab:attr name="size" value="52428800"/>
            <torznab:attr name="seeders" value="{12 - i}"/>
            <torznab:attr name="peers" value="{15 - i}"/>
            <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:{i:040x}&amp;dn=x"/></item>"""
            for i, t in enumerate(titles)
        ).replace("torznab:attr", "newznab:attr")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://torznab.com/schemas/2015/feed">
<channel><title>Test Tracker</title>{items}</channel></rss>""".encode()
    items = "".join(
        f"""<item><title>{t}</title><guid>nz-{t}</guid>
        <link>{NZ_URL}/getnzb/{i}.nzb?apikey={NZ_KEY}&amp;i=1</link>
        <pubDate>Fri, 09 Oct 2026 10:00:00 +0000</pubDate>
        <newznab:attr name="category" value="7010"/>
        <newznab:attr name="size" value="52428800"/></item>"""
        for i, t in enumerate(titles)
    )
    limits = '<newznab:apilimits apicurrent="3" apimax="50" grabcurrent="0" grabmax="5"/>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel><title>Test Indexer</title>{limits if apilimits else ""}{items}</channel></rss>""".encode()


class FakeNewznab:
    """Callable transport. Records every request; answers t=caps, t=search and NZBs."""

    def __init__(self, torznab=False):
        self.torznab = torznab
        self.calls = []
        self.search_titles = []
        self.rss_titles = []
        self.fail_with = None
        self.nzb = FakeProwlarr().nzb

    def __call__(self, url, headers, timeout):
        parts = urllib.parse.urlsplit(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.calls.append({"url": url, "path": parts.path, "query": query})
        if self.fail_with:
            status, body = self.fail_with
            self.fail_with = None
            return status, body
        if query.get("apikey") != NZ_KEY:
            return 200, b'<error code="100" description="Incorrect user credentials"/>'
        if parts.path == "/api" and query.get("t") == "caps":
            return 200, CAPS
        if parts.path == "/api" and query.get("t") == "search":
            titles = self.search_titles if "q" in query else self.rss_titles
            return 200, items_xml(titles, torznab=self.torznab)
        if parts.path.startswith("/getnzb/"):
            return 200, self.nzb
        return 404, b"not found"
