"""Grab, monitor, import and failed-download handling.

Safety (CLAUDE.md):
- Jobs go only into PeriodiCool's category, and only jobs listed in `grabs`
  (by job ID) are ever read. Nothing here changes other jobs or settings.
- Imports go through the library apply path: move or copy, never overwrite,
  every move logged.
- The one delete: after a verified import, the job's leftover folder in the
  downloads folder is removed (`remove_leftovers`), under the checks in
  `_safe_to_remove`. On any doubt it is left alone.
"""

from __future__ import annotations

import calendar
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from periodicool import history, pdf_text
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.registry import CLIENT_PROTOCOLS, KINDS
from periodicool.config import Settings
from periodicool.decisions import evaluate, format_rank, load_options
from periodicool.library import (
    DEFAULT_TEMPLATE,
    TEMPLATE_KEY,
    apply_item,
    get_setting,
    pdf_options,
    walk,
)
from periodicool.matching import TitleIndex, match_title
from periodicool.models import (
    ACTIVE_GRAB,
    BlockedRelease,
    Grab,
    GrabStatus,
    HistoryEvent,
    ImportItem,
    Indexer,
    Issue,
    IssueStatus,
    ItemAction,
    ItemStatus,
    Magazine,
    Release,
    Scan,
    ScanKind,
)
from periodicool.models import DownloadClient as DownloadClientRow
from periodicool.naming import UnsafePathError, render
from periodicool.parser import FORMATS, parse_release
from periodicool.prowlarr import ProwlarrError, RateLimited
from periodicool.schedule import identify

log = logging.getLogger(__name__)

AUTO_KEY = "grab.auto"
MAX_PER_RUN_KEY = "grab.max_per_run"
LEFTOVERS_KEY = "downloads.remove_leftovers"
MAX_TRIES_PER_ISSUE = 3

_NZB_NS = "{http://www.newzbin.com/DTD/2003/nzb}"
_NOT_MAGAZINE = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "nfo",
    "txt",
    "sfv",
    "par2",
    "nzb",
    "url",
    "exe",
    "srr",
    "md5",
    "sha1",
    "db",
    "htm",
    "html",
}
_ISSUE_FIELDS = (
    "year",
    "month",
    "month_end",
    "day",
    "season",
    "issue_number",
    "volume_number",
    "special",
    "cover_date",
)


def make_download_client(session: Session, transport=None, api_key: str | None = None):
    """The first enabled SABnzbd entry as a client, or None. `api_key` overrides the
    environment fallback (tests)."""
    from periodicool.sources import SAB_KEY_ENV, build_client

    row = session.scalars(
        select(DownloadClientRow)
        .where(DownloadClientRow.enabled.is_(True), DownloadClientRow.kind == "sabnzbd")
        .order_by(DownloadClientRow.priority, DownloadClientRow.id)
    ).first()
    if row is None:
        return None
    env = {SAB_KEY_ENV: api_key} if api_key else None
    return build_client(session, row, {"sab": transport}, env)


# Sources or single clients: callers pass either a sources.Sources (every list
# entry) or one client object (tests, the CLI).


def _indexer_for(prowlarr, release: Release):
    if hasattr(prowlarr, "indexer_for"):
        return prowlarr.indexer_for(release)
    return prowlarr


def _client_for(client, protocol: str | None, entry: Indexer | None = None):
    """The client a release goes to: the indexer entry's own download client when it
    names one (only that one, as in Sonarr), else the first enabled client for the
    protocol."""
    pinned = entry.download_client_id if entry is not None else None
    if pinned is not None:
        if hasattr(client, "client_for_id"):
            return client.client_for_id(pinned)
        return client if getattr(client, "row_id", None) == pinned else None
    if hasattr(client, "client_for"):
        return client.client_for(protocol)
    return client


def _client_for_grab(client, g: Grab):
    if hasattr(client, "client_for_grab"):
        return client.client_for_grab(g)
    if client is not None and getattr(client, "row_id", None) not in (None, g.client_id):
        return None
    return client


def _download_clients(client) -> list:
    if hasattr(client, "clients"):
        return client.clients
    return [] if client is None else [client]


def client_label(c) -> str:
    return getattr(c, "display_name", None) or c.name


# NZB check


def nzb_file_names(nzb: bytes) -> list[str]:
    root = ET.fromstring(nzb)
    names = []
    for f in root.iter(f"{_NZB_NS}file"):
        subject = f.get("subject", "")
        quoted = re.search(r'"([^"]+)"', subject)
        names.append((quoted.group(1) if quoted else subject).strip())
    return names


def check_nzb(nzb: bytes) -> str | None:
    """None if the NZB may hold a magazine file, else the reason it cannot."""
    try:
        names = nzb_file_names(nzb)
    except ET.ParseError:
        return "not a valid NZB"
    return check_names(names, "NZB")


def check_torrent(torrent: bytes) -> str | None:
    """None if the torrent may hold a magazine file, else the reason it cannot."""
    from periodicool.bencode import BencodeError, file_names

    try:
        names = file_names(torrent)
    except BencodeError:
        return "not a valid torrent file"
    return check_names(names, "torrent")


def check_names(names: list[str], what: str) -> str | None:
    if not names:
        return f"{what} lists no files"
    exts = set()
    for name in names:
        m = re.search(r"\.([A-Za-z0-9]{2,5})$", name)
        ext = m.group(1).lower() if m else ""
        ext = "par2" if re.fullmatch(r"vol\d+\+\d+", ext) else ext
        if ext in FORMATS or any(f".{f}" in name.lower() for f in FORMATS):
            return None
        exts.add(ext)
    if exts and exts <= _NOT_MAGAZINE:
        return f"{what} holds no pdf/epub/cbz/cbr (only {', '.join(sorted(exts))})"
    return None  # archives or obfuscated names: unknown until downloaded


# Blocklist


def normalized_title(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.lower()).strip()


def blocklist(
    session: Session,
    release: Release | None,
    title: str,
    magazine_id,
    issue_key,
    reason: str,
    indexer_id=None,
    record: bool = True,
    info_hash: str | None = None,
) -> BlockedRelease:
    row = BlockedRelease(
        guid=release.guid if release else None,
        info_hash=info_hash.lower() if info_hash else None,
        title=title,
        indexer_id=release.indexer_id if release else indexer_id,
        magazine_id=magazine_id,
        issue_key=issue_key,
        reason=reason[:1000],
    )
    session.add(row)
    if record:
        history.record(
            session,
            HistoryEvent.BLOCKLISTED,
            magazine_id=magazine_id,
            issue_key=issue_key,
            source_title=title,
            reason=reason[:1000],
        )
    log.info("Blocklisted %s: %s", title, reason)
    return row


def _grab_hash(g: Grab) -> str | None:
    """A torrent grab's job ID is its info hash (a blackhole's is "hash|name")."""
    return g.job_id.split("|", 1)[0] if g.protocol == "torrent" and g.job_id else None


def blocked_hash(session: Session, info_hash: str) -> BlockedRelease | None:
    return session.scalars(
        select(BlockedRelease).where(BlockedRelease.info_hash == info_hash.lower())
    ).first()


def blocked(session: Session, guid: str | None, title: str, magazine_id: int | None):
    if guid:
        row = session.scalars(select(BlockedRelease).where(BlockedRelease.guid == guid)).first()
        if row:
            return row
    if magazine_id is None:
        return None
    norm = normalized_title(title)
    for row in session.scalars(
        select(BlockedRelease).where(BlockedRelease.magazine_id == magazine_id)
    ):
        if normalized_title(row.title) == norm:
            return row
    return None


# Grabbing


@dataclass
class GrabResult:
    release: Release
    grab: Grab | None = None
    reasons: list[str] = field(default_factory=list)

    client_name: str = ""

    @property
    def ok(self) -> bool:
        return self.grab is not None


def _issue(session: Session, mag_id: int, key: str) -> Issue | None:
    return session.scalars(
        select(Issue).where(Issue.magazine_id == mag_id, Issue.key == key)
    ).one_or_none()


def _active_grab(session: Session, mag_id: int, key: str) -> Grab | None:
    return session.scalars(
        select(Grab).where(
            Grab.magazine_id == mag_id, Grab.issue_key == key, Grab.status.in_(ACTIVE_GRAB)
        )
    ).first()


def grab(
    session: Session,
    release: Release,
    prowlarr,
    client,
    today: date,
    check_limits: bool = True,
) -> GrabResult:
    """Send one release to the download client, after checking it again.

    `prowlarr` and `client` are a sources.Sources or single clients; the NZB comes
    from the release's own indexer entry and goes to the highest-priority enabled
    client for its protocol."""
    from periodicool.search import indexer_limits

    result = GrabResult(release)
    magazines = list(session.scalars(select(Magazine)))
    entry = session.get(Indexer, release.source_id) if release.source_id else None
    entry_settings = (entry.settings if entry is not None else None) or {}
    ev = evaluate(
        session,
        release.title,
        magazines,
        today,
        size=release.size,
        protocol=release.protocol,
        published=release.publish_date,
        options=load_options(session),
        guid=release.guid,
        seeders=release.seeders,
        min_seeders=entry_settings.get("min_seeders"),
    )
    if not ev.accepted:
        result.reasons = list(ev.reasons)
        return result
    mag = ev.magazine
    if active := _active_grab(session, mag.id, ev.issue_key):
        result.reasons.append(f"{ev.issue_key} is already downloading ({active.title})")
    indexer = _indexer_for(prowlarr, release)
    if indexer is None:
        result.reasons.append("the indexer this release came from is removed or disabled")
    protocol = release.protocol or "usenet"
    target = _client_for(client, protocol, entry)
    category = getattr(target, "category", "") if target is not None else ""
    if target is None and entry is not None and entry.download_client_id is not None:
        pinned = session.get(DownloadClientRow, entry.download_client_id)
        name = f" {pinned.name}" if pinned else ""
        result.reasons.append(f"the indexer's download client{name} is not available")
    elif target is None:
        result.reasons.append(f"no enabled {protocol} download client")
    elif (takes := CLIENT_PROTOCOLS.get(target.name)) and takes != protocol:
        result.reasons.append(
            f"the indexer's download client {client_label(target)} takes {takes}, not {protocol}"
        )
    elif not category and getattr(KINDS.get(target.name), "has_category", True):
        result.reasons.append(
            f"no category set for {client_label(target)} (Settings, Download clients)"
        )
    if not release.download_url:
        result.reasons.append("release has no download link")
    if check_limits and indexer is not None:
        wanted_id = release.indexer_id if getattr(indexer, "kind", "") == "prowlarr" else 0
        if wanted_id is not None:
            for lim in indexer_limits(session, indexer):
                if lim.id == wanted_id and lim.grabs_left == 0:
                    result.reasons.append(f"{lim.name} grab limit reached ({lim.grab_limit} today)")
    if result.reasons:
        log.info("Not grabbed %s: %s", release.title, "; ".join(result.reasons))
        return result

    try:
        kind, payload = indexer.fetch_download(release.download_url, release.indexer_id)
    except ProwlarrError as exc:
        result.reasons.append(f"download failed: {exc}")
        log.warning("Not grabbed %s: download failed: %s", release.title, exc)
        return result
    got = "usenet" if kind == "nzb" else "torrent"
    if got != protocol:
        result.reasons.append(f"the indexer sent a {got} download for a {protocol} release")
        return result
    if kind != "magnet" and (
        bad := check_nzb(payload) if kind == "nzb" else check_torrent(payload)
    ):
        blocklist(session, release, release.title, mag.id, ev.issue_key, bad)
        result.reasons.append(bad)
        return result
    if kind != "nzb":
        from periodicool import bencode

        try:
            digest = (
                bencode.info_hash(payload) if kind == "torrent" else bencode.magnet_hash(payload)
            )
        except bencode.BencodeError:
            digest = None
        # Sonarr's "reject blocklisted torrent hashes while grabbing": the same torrent
        # under another name or from another indexer.
        rejecting = digest and entry_settings.get("reject_blocklisted_hashes")
        if rejecting and (hit := blocked_hash(session, digest)):
            result.reasons.append(f"this torrent's hash is blocklisted ({hit.title})")
            return result
        # A torrent is tracked by its hash, so the same one is never sent twice.
        same = or_(Grab.job_id == digest, Grab.job_id.like(f"{digest}|%"))
        if digest and (sent := session.scalars(select(Grab).where(same)).first()):
            result.reasons.append(
                f"this torrent was sent before, as {sent.title} ({sent.status.value})"
            )
            return result
    extra = add_options(target, _cover_date(session, mag.id, ev), today)
    try:
        if kind == "nzb":
            job_id = target.add_nzb(payload, release.title, category, **extra)
        else:
            job_id = target.add_torrent(
                release.title,
                category,
                torrent=payload if kind == "torrent" else None,
                magnet=payload if kind == "magnet" else None,
                seed_ratio=_number(entry_settings.get("seed_ratio")),
                seed_time_min=_number(entry_settings.get("seed_time_min")),
                **extra,
            )
    except (ClientError, AttributeError) as exc:
        result.reasons.append(f"not sent to {client_label(target)}: {exc}")
        return result

    issue = _issue(session, mag.id, ev.issue_key)
    if issue is None:
        fields = {k: ev.fields.get(k) for k in _ISSUE_FIELDS}
        fields["special"] = bool(fields["special"])
        issue = Issue(
            magazine_id=mag.id, key=ev.issue_key, expected=False, monitored=True, **fields
        )
        session.add(issue)
    issue.status = IssueStatus.GRABBED
    result.grab = Grab(
        release_id=release.id,
        magazine_id=mag.id,
        issue_key=ev.issue_key,
        title=release.title,
        indexer_id=release.indexer_id,
        client=target.name,
        client_id=getattr(target, "row_id", None),
        job_id=job_id,
        protocol=protocol,
        category=category,
        status=GrabStatus.QUEUED,
        progress=0.0,
    )
    session.add(result.grab)
    result.client_name = client_label(target)
    session.flush()
    history.record_grab(
        session,
        HistoryEvent.GRABBED,
        result.grab,
        indexer_id=release.indexer_id,
        size=release.size,
        guid=release.guid,
    )
    log.info("Grabbed %s for %s %s as job %s", release.title, mag.title, ev.issue_key, job_id)
    return result


def _number(value):
    return value if isinstance(value, int | float) and value >= 0 else None


RECENT_DAYS = 14  # Sonarr's line between recent and older priority


def _cover_date(session: Session, mag_id: int, ev) -> date | None:
    """The day an issue counts from for recent or older priority. A month-only date
    counts from the month's last day (an October issue is current all October)."""
    issue = _issue(session, mag_id, ev.issue_key)
    fields = ev.fields
    if issue is not None and issue.year:
        fields = {
            "year": issue.year,
            "month": issue.month,
            "month_end": issue.month_end,
            "day": issue.day,
        }
    year, month = fields.get("year"), fields.get("month_end") or fields.get("month")
    if not year or not month:
        return None
    try:
        if fields.get("day") and not fields.get("month_end"):
            return date(year, month, fields["day"])
        return date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        return None


def add_options(client, cover: date | None, today: date) -> dict:
    """The add options a client entry was set up with: priority (recent or older, by
    the issue's cover date, as Sonarr does), add paused, and directory. Only options
    the user set are passed, so each client keeps its own defaults otherwise."""
    opts = getattr(client, "options", None) or {}
    recent = cover is None or (today - cover).days <= RECENT_DAYS
    out: dict = {}
    priority = opts.get("recent_priority" if recent else "older_priority")
    if priority not in (None, ""):
        out["priority"] = str(priority)
    state = opts.get("initial_state")
    if opts.get("add_paused") or state == "pause":
        out["paused"] = True
    if state == "force_start":
        out["force_start"] = True
    if opts.get("directory"):
        out["directory"] = str(opts["directory"])
    return out


def _candidates(session: Session, mag_id: int | None = None, key: str | None = None):
    """Accepted releases per issue, best first."""
    query = select(Release).where(Release.accepted.is_(True))
    if mag_id is not None:
        query = query.where(Release.magazine_id == mag_id, Release.issue_key == key)
    by_issue: dict[tuple[int, str], list[Release]] = {}
    for rel in session.scalars(query):
        by_issue.setdefault((rel.magazine_id, rel.issue_key), []).append(rel)
    # Indexer entry priority breaks ties after format and agreement, as in Sonarr.
    priority = dict(session.execute(select(Indexer.id, Indexer.priority)).all())

    def order(r: Release) -> tuple:
        return (r.rank, priority.get(r.source_id, 50))

    return {k: sorted(v, key=order) for k, v in by_issue.items()}


def _grab_first(session, releases, prowlarr, client, today) -> GrabResult | None:
    last = None
    for rel in releases[:MAX_TRIES_PER_ISSUE]:
        last = grab(session, rel, prowlarr, client, today)
        if last.ok:
            return last
    return last


def auto_grab(session: Session, prowlarr, client, today: date) -> list[GrabResult]:
    """Grab the best release per wanted issue. Off unless grab.auto is true."""
    if not get_setting(session, AUTO_KEY, False) or prowlarr is None or client is None:
        return []
    limit = int(get_setting(session, MAX_PER_RUN_KEY, 10))
    results = []
    for (mag_id, key), releases in _candidates(session).items():
        if sum(r.ok for r in results) >= limit:
            break
        if _active_grab(session, mag_id, key):
            continue
        try:
            res = _grab_first(session, releases, prowlarr, client, today)
        except RateLimited:
            break
        if res is not None:
            results.append(res)
    return results


# Monitoring and importing


@dataclass
class MonitorResult:
    updated: int = 0
    imported: list[Grab] = field(default_factory=list)
    failed: list[Grab] = field(default_factory=list)
    review: list[Grab] = field(default_factory=list)
    removed: list[Grab] = field(default_factory=list)
    retried: list[Grab] = field(default_factory=list)


def map_path(remote: str, path_map: list) -> str:
    """Translate a path as the download client sees it to how we see it."""
    norm = remote.replace("\\", "/")
    best = None
    for pair in path_map or []:
        src, dst = pair[0].replace("\\", "/").rstrip("/"), pair[1]
        if (norm == src or norm.startswith(src + "/")) and (
            best is None or len(src) > len(best[0])
        ):
            best = (src, dst)
    if best is None:
        return remote
    rest = norm[len(best[0]) :].lstrip("/")
    return str(Path(best[1]) / rest) if rest else best[1]


def monitor(session: Session, clients, settings: Settings, prowlarr, today: date) -> MonitorResult:
    """Ask each enabled download client about PeriodiCool's own jobs on it, then
    import, fail or retry. `clients` is a sources.Sources or one client."""
    result = MonitorResult()
    for client in _download_clients(clients):
        _monitor_client(session, client, clients, settings, prowlarr, today, result)
    session.flush()
    return result


def _monitor_client(session, client, clients, settings, prowlarr, today, result) -> None:
    row_id = getattr(client, "row_id", None)
    owner = Grab.client_id == row_id if row_id is not None else Grab.client == client.name
    active = list(session.scalars(select(Grab).where(owner, Grab.status.in_(ACTIVE_GRAB))))
    by_category: dict[str, list[Grab]] = {}
    for g in active:
        by_category.setdefault(g.category, []).append(g)
    for category, grabs in by_category.items():
        states = {s.job_id: s for s in client.jobs([g.job_id for g in grabs], category)}
        for g in grabs:
            state = states.get(g.job_id)
            if state is None:
                if g.status is GrabStatus.COMPLETED and g.storage_path:
                    _import(session, g, settings, prowlarr, clients, today, result)
                    continue
                g.status, g.error = GrabStatus.REMOVED, "job no longer in the download client"
                _issue_wanted(session, g)
                history.record_grab(session, HistoryEvent.DOWNLOAD_REMOVED, g, reason=g.error)
                result.removed.append(g)
                continue
            g.client_status, g.progress = state.client_status, state.progress
            if state.size_bytes is not None:
                g.size_bytes = state.size_bytes
            g.size_left_bytes, g.time_left_s = state.size_left_bytes, state.time_left_s
            result.updated += 1
            if state.status is JobStatus.FAILED:
                message = state.fail_message or state.client_status
                if local_failure(message):
                    _local_fail(session, g, message)
                    result.failed.append(g)
                    continue
                reason = f"download failed: {message}"
                if new := fail(session, g, reason, prowlarr, clients, today):
                    result.retried.append(new)
                result.failed.append(g)
            elif state.status is JobStatus.COMPLETED:
                g.status, g.storage_path = GrabStatus.COMPLETED, state.storage_path
                g.completed_at = g.completed_at or datetime.now(UTC)
                _import(session, g, settings, prowlarr, clients, today, result)
            elif state.status is JobStatus.DOWNLOADING:
                g.status = GrabStatus.DOWNLOADING


def _import(session, g, settings, prowlarr, client, today, result) -> None:
    outcome = import_download(session, g, settings)
    if outcome == "imported":
        result.imported.append(g)
        after_import(_client_for_grab(client, g), g)
        from periodicool.covers import refresh_covers

        refresh_covers(session, settings.library_dir, settings.config_dir, g.magazine_id)
    elif outcome == "failed":
        reason = g.error or "import failed"
        if new := fail(
            session, g, reason, prowlarr, client, today, event=HistoryEvent.IMPORT_FAILED
        ):
            result.retried.append(new)
        result.failed.append(g)
    else:
        result.review.append(g)


def after_import(client, g: Grab) -> None:
    """What the client entry asks for once our download is in the library: a torrent
    moves to the post-import category, or a usenet job leaves the client's history
    (remove completed). Only our own job, and never its files. A failure here is
    logged and does not undo the import."""
    if client is None:
        return
    opts = getattr(client, "options", None) or {}
    try:
        if g.protocol == "torrent" and opts.get("post_import_category"):
            client.set_category(g.job_id, g.category, str(opts["post_import_category"]))
            log.info(
                "Moved %s to %s in %s", g.title, opts["post_import_category"], client_label(client)
            )
        elif g.protocol != "torrent" and opts.get("remove_completed"):
            done = [s for s in client.jobs([g.job_id], g.category) if s.in_history]
            if done and done[0].status is JobStatus.COMPLETED:
                client.remove(g.job_id, g.category)
                log.info("Removed %s from %s history", g.title, client_label(client))
    except (ClientError, AttributeError) as exc:
        log.warning("After importing %s, %s: %s", g.title, client_label(client), exc)


def retry_import(
    session: Session,
    grab_id: int,
    settings: Settings,
    prowlarr,
    client,
    today: date,
) -> str:
    """Try the import again for a finished download waiting in review (after the
    cause, such as a folder permission, was fixed). Returns imported, review or failed."""
    g = session.get(Grab, grab_id)
    if g is None:
        raise ValueError(f"no grab {grab_id}")
    if g.status is not GrabStatus.REVIEW or not g.storage_path:
        raise ValueError("only a finished download waiting in review can be imported again")
    g.status, g.error = GrabStatus.COMPLETED, None
    result = MonitorResult()
    _import(session, g, settings, prowlarr, client, today, result)
    session.flush()
    if result.imported:
        return "imported"
    return "failed" if result.failed else "review"


def _issue_wanted(session: Session, g: Grab) -> None:
    issue = _issue(session, g.magazine_id, g.issue_key) if g.magazine_id else None
    if issue is not None and not issue.file_path:
        issue.status = IssueStatus.WANTED


# SABnzbd messages that point at the user's own setup (folders, disk, permissions),
# not at the release. Blocklisting the release for these would be wrong, and an
# automatic retry would fail the same way.
_LOCAL_FAILURES = (
    "post-processing was aborted",
    "cannot create final folder",
    "permission denied",
    "no space left",
    "disk full",
    "not enough free space",
    "read-only file system",
    "write error",
    "missingfiles",  # qBittorrent: its files were moved or deleted on that side
)


def local_failure(message: str | None) -> bool:
    lowered = (message or "").lower()
    return any(words in lowered for words in _LOCAL_FAILURES)


def _local_fail(session: Session, g: Grab, message: str) -> None:
    """A download that failed because of a problem on this side: not blocklisted,
    not retried. The issue is wanted again, so it can be grabbed once fixed."""
    reason = (
        f"{g.client_label} could not finish the job: {message}. This is usually a folder "
        f"permission or disk space problem on the {g.client_label} side, not a bad release. "
        f"Check {g.client_label}'s warnings, fix the cause, then grab the release again."
    )
    g.status, g.error = GrabStatus.FAILED, reason[:2000]
    _issue_wanted(session, g)
    history.record_grab(
        session,
        HistoryEvent.DOWNLOAD_FAILED,
        g,
        reason=reason[:1000],
        blocklisted=False,
        local=True,
    )
    session.flush()
    log.warning(
        "Grab %s failed on the %s side (not blocklisted): %s", g.title, g.client_label, message
    )


def fail(
    session: Session,
    g: Grab,
    reason: str,
    prowlarr,
    client,
    today: date,
    event: HistoryEvent = HistoryEvent.DOWNLOAD_FAILED,
) -> Grab | None:
    """Blocklist the release, free the issue, and grab the next best release once."""
    blocklist(
        session,
        g.release,
        g.title,
        g.magazine_id,
        g.issue_key,
        reason,
        g.indexer_id,
        record=False,
        info_hash=_grab_hash(g),
    )
    g.status, g.error = GrabStatus.FAILED, reason[:2000]
    _issue_wanted(session, g)
    history.record_grab(session, event, g, reason=reason[:1000], blocklisted=True)
    session.flush()
    log.warning("Grab %s failed: %s", g.title, reason)
    if prowlarr is None or client is None or g.magazine_id is None:
        return None
    releases = _candidates(session, g.magazine_id, g.issue_key).get(
        (g.magazine_id, g.issue_key), []
    )
    try:
        res = _grab_first(session, releases, prowlarr, client, today)
    except RateLimited:
        return None
    return res.grab if res and res.ok else None


_PATH_HINT = (
    "Mount the download client's folder in PeriodiCool at the same path the client "
    "uses (for example the same host folder at /data in both containers), or set a "
    "remote path mapping in Settings, Download clients. Then press Retry import."
)


def import_download(session: Session, g: Grab, settings: Settings) -> str:
    """Move the finished file into the library. Returns imported, review or failed."""
    downloads = settings.downloads_dir.resolve()
    row = session.get(DownloadClientRow, g.client_id) if g.client_id else None
    local = Path(map_path(g.storage_path or "", row.path_map if row else [])).resolve()
    if local == downloads or not local.is_relative_to(downloads):
        why = (
            f"{g.client_label} put the download at {local}, which is outside the downloads folder "
            f"{downloads}. {_PATH_HINT}"
        )
        return _review(session, g, why)
    if not local.exists():
        return _review(
            session,
            g,
            f"{g.client_label} reports the download at {g.storage_path}, but PeriodiCool cannot "
            f"see {local}. {_PATH_HINT}",
        )

    files = [local] if local.is_file() else walk(local)[0]
    files = [f for f in files if f.suffix.lower().lstrip(".") in FORMATS]
    if not files:
        g.error = "no pdf/epub/cbz/cbr in download"
        return "failed"
    mag = session.get(Magazine, g.magazine_id)
    issue = _issue(session, g.magazine_id, g.issue_key)
    if mag is None or issue is None:
        return _review(session, g, "magazine or issue no longer exists")

    magazines = list(session.scalars(select(Magazine)))
    titles = TitleIndex.from_magazines(magazines)

    def score(f: Path):
        parsed = parse_release(f.stem, titles)
        ext = f.suffix.lower().lstrip(".")
        return [*format_rank(ext, parsed.true_pdf, mag.format_profile), -f.stat().st_size]

    best = min(files, key=score)
    parsed = parse_release(best.stem, titles)
    if not parsed.reasons and match_title(parsed, mag.title_spec()).accepted:
        ident = identify(parsed, mag)
        if ident.confident and ident.key != g.issue_key:
            why = f"file {best.name} looks like {ident.key}, grabbed for {g.issue_key}"
            return _review(session, g, why)

    # Edition check: one file per download, so the PDF is always read when the
    # magazine has an ISSN. Another tracked magazine's ISSN means the wrong edition.
    pdf_issn = None
    if mag.issn and best.suffix.lower() == ".pdf" and pdf_options(session).enabled:
        clues = pdf_text.read_clues(best, pdf_options(session).pages)
        if clues and clues.issns:
            if mag.issn in clues.issns:
                pdf_issn = "matches"
            else:
                others = [m.title for m in magazines if m.issn and m.issn in clues.issns]
                if others:
                    why = f"the PDF's ISSN {clues.issns[0]} belongs to {others[0]}, not {mag.title}"
                    return _review(session, g, why)
                pdf_issn = f"unknown {clues.issns[0]}"

    ext = best.suffix.lower().lstrip(".")
    fields = {k: getattr(issue, k) for k in _ISSUE_FIELDS}
    template = get_setting(session, TEMPLATE_KEY, DEFAULT_TEMPLATE)
    try:
        target = render(template, mag, fields, ext, key=g.issue_key)
    except UnsafePathError as exc:
        return _review(session, g, str(exc))

    scan = Scan(kind=ScanKind.DOWNLOAD, root=str(local), counts={})
    session.add(scan)
    session.flush()
    src = str(best)
    item = session.scalars(select(ImportItem).where(ImportItem.source_path == src)).one_or_none()
    item = item or ImportItem(source_path=src)
    st = best.stat()
    item.scan_id, item.origin, item.size, item.mtime = (
        scan.id,
        ScanKind.DOWNLOAD,
        st.st_size,
        st.st_mtime,
    )
    item.parsed = {
        "fields": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items()},
        "format": ext,
        "grab_id": g.id,
        "resolved": True,
    }
    item.magazine_id, item.issue_key, item.target_path = mag.id, g.issue_key, target.as_posix()
    item.action, item.status, item.reason_code, item.reason = (
        ItemAction.MOVE,
        ItemStatus.PENDING,
        None,
        f"download {g.job_id}",
    )
    item.keep_source = g.protocol == "torrent"
    session.add(item)
    session.flush()
    g.import_item_id = item.id

    library_root = settings.library_dir.resolve()
    apply_item(session, item, library_root, {"moved": 0, "kept": 0, "failed": 0, "review": 0})
    if item.status is not ItemStatus.DONE or item.action is ItemAction.REVIEW:
        return _review(session, g, item.reason or "import did not complete")
    final = library_root / item.target_path
    if not final.is_file() or final.stat().st_size != st.st_size:
        return _review(session, g, f"imported file {final} missing or wrong size")
    g.status, g.imported_at, g.error = GrabStatus.IMPORTED, datetime.now(UTC), None
    history.record_grab(
        session,
        HistoryEvent.IMPORTED,
        g,
        path=item.target_path,
        size=st.st_size,
        format=ext,
        source_path=str(best),
        pdf_issn=pdf_issn,
    )
    log.info("Imported %s to %s", best, final)
    # A torrent keeps seeding from its folder: never clean that up.
    if g.protocol != "torrent" and get_setting(session, LEFTOVERS_KEY, True) and local.is_dir():
        if why := _safe_to_remove(local, downloads, library_root):
            log.warning("Leftovers kept at %s: %s", local, why)
        else:
            try:
                shutil.rmtree(local)
                log.info("Removed leftover folder of job %s: %s", g.job_id, local)
            except OSError as exc:  # the import already succeeded; only cleanup failed
                log.warning("Leftovers kept at %s, could not remove them: %s", local, exc)
    return "imported"


def _review(session: Session, g: Grab, reason: str) -> str:
    g.status, g.error = GrabStatus.REVIEW, reason[:2000]
    history.record_grab(session, HistoryEvent.IMPORT_REVIEW, g, reason=reason[:1000])
    log.warning("Grab %s needs review: %s", g.title, reason)
    return "review"


def _safe_to_remove(folder: Path, downloads: Path, library_root: Path) -> str | None:
    """None if this job folder may be removed, else why not."""
    if folder == downloads or not folder.is_relative_to(downloads):
        return "not strictly inside the downloads folder"
    if folder.is_relative_to(library_root) or library_root.is_relative_to(folder):
        return "overlaps the library"
    if folder.is_symlink():
        return "is a symlink"
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            if Path(dirpath, name).is_symlink():
                return f"contains a symlink ({name})"
    return None


def remove_grab(session: Session, grab_id: int, clients, add_to_blocklist: bool = False) -> Grab:
    """Remove one of our jobs from the download client (the queue's Remove button).

    Only grabs PeriodiCool made are touched, only on the client entry the grab went
    to, and the client checks the category again.
    """
    g = session.get(Grab, grab_id)
    if g is None:
        raise ValueError(f"no grab {grab_id}")
    client = _client_for_grab(clients, g)
    if client is None or g.client != client.name:
        raise ValueError(f"grab {grab_id} went to a download client that is removed or disabled")
    if g.status not in (*ACTIVE_GRAB, GrabStatus.REVIEW):
        raise ValueError(f"grab {grab_id} is {g.status.value}; nothing to remove")
    found = client.remove(g.job_id, g.category)
    g.status, g.error = GrabStatus.REMOVED, "removed by user"
    g.size_left_bytes = g.time_left_s = None
    _issue_wanted(session, g)
    if add_to_blocklist:
        blocklist(
            session,
            g.release,
            g.title,
            g.magazine_id,
            g.issue_key,
            "removed by user",
            g.indexer_id,
            record=False,
            info_hash=_grab_hash(g),
        )
    history.record_grab(
        session,
        HistoryEvent.DOWNLOAD_REMOVED,
        g,
        reason="removed by user",
        blocklisted=add_to_blocklist or None,
        in_client=found,
    )
    session.flush()
    log.info("Removed %s (job %s) from %s", g.title, g.job_id, client_label(client))
    return g
