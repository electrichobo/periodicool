"""The indexer and download-client lists, turned into working clients.

Everything that talks to an indexer or a download client gets it from here, so
no other module assumes a single Prowlarr or a single SABnzbd.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool.library import get_setting
from periodicool.models import ApiCall, DownloadClient, Grab, Indexer, Release

PROWLARR_KEY_ENV = "PROWLARR_API_KEY"
SAB_KEY_ENV = "SABNZBD_API_KEY"
INDEXER_KINDS = {"prowlarr": "Prowlarr", "newznab": "Newznab", "torznab": "Torznab"}
CLIENT_KINDS = {"sabnzbd": "SABnzbd", "qbittorrent": "qBittorrent"}
# The protocol each kind downloads.
CLIENT_PROTOCOLS = {"sabnzbd": "usenet", "qbittorrent": "torrent"}
MIN_INTERVAL_KEY = "search.min_interval_s"


def _first_id(session: Session, model, kind: str) -> int | None:
    return session.scalar(select(model.id).where(model.kind == kind).order_by(model.id).limit(1))


def indexer_key(session: Session, row: Indexer, env: Mapping[str, str] | None = None) -> str:
    """The row's own key; for the first Prowlarr entry, PROWLARR_API_KEY when it has none."""
    env = os.environ if env is None else env
    if row.api_key:
        return row.api_key
    if row.kind == "prowlarr" and row.id == _first_id(session, Indexer, "prowlarr"):
        return env.get(PROWLARR_KEY_ENV, "")
    return ""


def client_key(session: Session, row: DownloadClient, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if row.kind == "qbittorrent":
        return row.password or ""
    if row.api_key:
        return row.api_key
    if row.kind == "sabnzbd" and row.id == _first_id(session, DownloadClient, "sabnzbd"):
        return env.get(SAB_KEY_ENV, "")
    return ""


def build_indexer(session: Session, row: Indexer, transports: Mapping | None = None, env=None):
    """A client for one indexer row, recording its calls; None without a key."""
    from periodicool.newznab import NewznabClient, TorznabClient
    from periodicool.prowlarr import ProwlarrClient

    transports = transports or {}
    key = indexer_key(session, row, env)
    if not row.url or not key:
        return None

    def record(**call) -> None:
        session.add(ApiCall(source_id=row.id, **call))

    common = {
        "transport": transports.get(row.kind),
        "min_interval": float(get_setting(session, MIN_INTERVAL_KEY, 2.0)),
        "on_call": record,
    }
    if row.kind == "prowlarr":
        client = ProwlarrClient(row.url, key, **common)
    elif row.kind == "newznab":
        client = NewznabClient(row.url, key, name=row.name, **common)
    elif row.kind == "torznab":
        common["transport"] = transports.get("torznab")
        client = TorznabClient(row.url, key, name=row.name, **common)
    else:
        return None
    client.source_id, client.source_name, client.row = row.id, row.name, row
    return client


def build_client(
    session: Session, row: DownloadClient, transports: Mapping | None = None, env=None
):
    from periodicool.clients.qbittorrent import QbittorrentClient
    from periodicool.clients.sabnzbd import SabnzbdClient

    transports = transports or {}
    if not row.url:
        return None
    if row.kind == "qbittorrent":
        # Username and password may be empty when qBittorrent skips login for this address.
        client = QbittorrentClient(
            row.url, row.username or "", row.password or "", transport=transports.get("qbit")
        )
    elif row.kind == "sabnzbd" and (key := client_key(session, row, env)):
        client = SabnzbdClient(row.url, key, transport=transports.get("sab"))
    else:
        return None
    client.row_id, client.display_name = row.id, row.name
    client.category, client.path_map = row.category, list(row.path_map or [])
    return client


class Sources:
    """The enabled indexers and download clients for one job or request."""

    def __init__(self, session: Session, transports: Mapping | None = None, env=None) -> None:
        self.session = session
        self.transports = dict(transports or {})
        self.env = env
        self._indexers = None
        self._clients = None

    @property
    def indexers(self) -> list:
        if self._indexers is None:
            rows = self.session.scalars(
                select(Indexer)
                .where(Indexer.enabled.is_(True))
                .order_by(Indexer.priority, Indexer.id)
            )
            built = (build_indexer(self.session, r, self.transports, self.env) for r in rows)
            self._indexers = [c for c in built if c is not None]
        return self._indexers

    @property
    def clients(self) -> list:
        if self._clients is None:
            rows = self.session.scalars(
                select(DownloadClient)
                .where(DownloadClient.enabled.is_(True))
                .order_by(DownloadClient.priority, DownloadClient.id)
            )
            built = (build_client(self.session, r, self.transports, self.env) for r in rows)
            self._clients = [c for c in built if c is not None]
        return self._clients

    def indexer_for(self, release: Release):
        """The indexer a release came from, to download its NZB."""
        for client in self.indexers:
            if client.source_id == release.source_id:
                return client
        return None

    def client_for(self, protocol: str | None):
        """The highest-priority enabled download client for a protocol."""
        wanted = protocol or "usenet"
        for client in self.clients:
            row = self.session.get(DownloadClient, client.row_id)
            if row is not None and row.protocol == wanted:
                return client
        return None

    def client_for_grab(self, grab: Grab):
        for client in self.clients:
            if client.row_id == grab.client_id:
                return client
        return None

    def protocols(self) -> set[str]:
        """Protocols with at least one enabled download client."""
        return {
            row.protocol
            for row in self.session.scalars(
                select(DownloadClient).where(DownloadClient.enabled.is_(True))
            )
        }

    def first_prowlarr(self):
        return next((c for c in self.indexers if getattr(c, "kind", "") == "prowlarr"), None)


def transports_from(state) -> dict:
    """Test transports kept on the app state, by kind."""
    return {
        "prowlarr": getattr(state, "prowlarr_transport", None),
        "newznab": getattr(state, "newznab_transport", None),
        "torznab": getattr(state, "torznab_transport", None),
        "sab": getattr(state, "sab_transport", None),
        "qbit": getattr(state, "qbit_transport", None),
    }
