"""Helpers that add indexer and download client list entries in tests."""

from periodicool.models import DownloadClient, Indexer


def add_indexer(session, url, key=None, name="Prowlarr", kind="prowlarr", **kw) -> Indexer:
    kw.setdefault("categories", [])
    kw.setdefault("settings", {})
    row = Indexer(name=name, kind=kind, url=url, api_key=key, **kw)
    session.add(row)
    session.flush()
    return row


def add_client(
    session, url, key=None, name="SABnzbd", category="mags", path_map=None, **kw
) -> DownloadClient:
    row = DownloadClient(
        name=name,
        kind="sabnzbd",
        url=url,
        api_key=key,
        category=category,
        path_map=path_map or [],
        **kw,
    )
    session.add(row)
    session.flush()
    return row
