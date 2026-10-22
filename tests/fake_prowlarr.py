"""A fake Prowlarr transport for tests. No network."""

import json
import re
import urllib.parse

KEY = "test-secret-key-123"
URL = "http://prowlarr.test:9696"

INDEXERS = [
    {
        "id": 1,
        "name": "Alpha",
        "protocol": "usenet",
        "enable": True,
        "capabilities": {"categories": [{"id": 7000, "subCategories": [{"id": 7010}]}]},
        "fields": [{"name": "baseSettings.queryLimit", "value": 100}],
    },
    {
        "id": 2,
        "name": "Beta",
        "protocol": "usenet",
        "enable": True,
        "capabilities": {"categories": [{"id": 7000, "subCategories": [{"id": 7010}]}]},
        "fields": [],
    },
    {
        "id": 3,
        "name": "Torrents",
        "protocol": "torrent",
        "enable": True,
        "capabilities": {"categories": [{"id": 7000}]},
        "fields": [],
    },
]


def release_json(title, guid=None, indexer_id=1, size=50 * 1048576, date="2026-10-10T00:00:00Z"):
    guid = guid or f"guid-{title}"
    return {
        "guid": guid,
        "title": title,
        "size": size,
        "publishDate": date,
        "indexerId": indexer_id,
        "indexer": "Alpha",
        "downloadUrl": f"{URL}/{indexer_id}/download?apikey={KEY}&link=abc",
        "infoUrl": f"https://indexer.test/details/{guid}",
        "categories": [{"id": 7010, "name": "Books/Mags"}],
        "protocol": "usenet",
    }


def newznab_xml(titles, apilimits=True):
    items = "".join(
        f"""<item><title>{t}</title><guid>rss-{t}</guid>
        <link>{URL}/1/download?apikey={KEY}&amp;link=x</link>
        <pubDate>Fri, 09 Oct 2026 10:00:00 +0000</pubDate>
        <newznab:attr name="category" value="7010"/>
        <newznab:attr name="size" value="52428800"/></item>"""
        for t in titles
    )
    limits = '<newznab:apilimits apicurrent="12" apimax="100" grabcurrent="1" grabmax="10"/>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel><title>Prowlarr</title>{limits if apilimits else ""}{items}</channel></rss>""".encode()


class FakeProwlarr:
    """Callable transport. Records every request; answers from the attributes below."""

    def __init__(self):
        self.calls = []
        self.search_results = []
        self.rss_titles = {1: [], 2: []}
        self.status = []
        self.stats = {"indexers": []}
        self.fail_with = None  # (status, body) for the next call
        # Set to a URL to make /download answer with a redirect, as Prowlarr does
        # for indexers set to "Redirect". The indexer then answers indexer_reply.
        self.download_redirect = None
        self.indexer_reply = (200, b"")
        self.nzb = (
            b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
            b'<file subject="&quot;issue.pdf&quot; yEnc"><segments/></file></nzb>'
        )

    def paths(self, kind=None):
        return [c for c in self.calls if kind is None or kind in c["path"]]

    def feeds(self):
        """Newznab RSS calls (/{id}/api), the ones that cost indexer API hits."""
        return [c for c in self.calls if re.fullmatch(r"/\d+/api", c["path"])]

    def __call__(self, url, headers, timeout):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        self.calls.append({"url": url, "path": parts.path, "query": query, "headers": headers})
        if self.fail_with:
            status, body = self.fail_with
            self.fail_with = None
            return status, body
        if parts.path == "/api/v1/indexer":
            return 200, json.dumps(INDEXERS).encode()
        if parts.path == "/api/v1/indexerstatus":
            return 200, json.dumps(self.status).encode()
        if parts.path == "/api/v1/indexerstats":
            return 200, json.dumps(self.stats).encode()
        if parts.path == "/api/v1/search":
            return 200, json.dumps(self.search_results).encode()
        if parts.netloc == "indexer.test":  # an indexer Prowlarr redirected to
            return self.indexer_reply
        if parts.path.endswith("/download"):
            if self.download_redirect:
                return 302, b"", {"Location": self.download_redirect}
            return 200, self.nzb
        if parts.path.endswith("/api"):
            indexer = int(parts.path.strip("/").split("/")[0])
            return 200, newznab_xml(self.rss_titles.get(indexer, []))
        return 404, b"not found"
