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

import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool import history
from periodicool.clients.base import ClientError, DownloadClient, JobStatus
from periodicool.clients.sabnzbd import SabnzbdClient
from periodicool.config import Settings
from periodicool.decisions import evaluate, format_rank, load_options
from periodicool.library import (
    DEFAULT_TEMPLATE,
    TEMPLATE_KEY,
    apply_item,
    get_setting,
    walk,
)
from periodicool.matching import match_title
from periodicool.models import (
    ACTIVE_GRAB,
    BlockedRelease,
    Grab,
    GrabStatus,
    HistoryEvent,
    ImportItem,
    Issue,
    IssueStatus,
    ItemAction,
    ItemStatus,
    Magazine,
    Release,
    Scan,
    ScanKind,
)
from periodicool.naming import UnsafePathError, render
from periodicool.parser import FORMATS, parse_release
from periodicool.prowlarr import ProwlarrClient, ProwlarrError, RateLimited
from periodicool.schedule import identify

log = logging.getLogger(__name__)

SAB_URL_KEY = "sabnzbd.url"
SAB_KEY_ENV = "SABNZBD_API_KEY"
CATEGORY_KEY = "sabnzbd.category"
PATH_MAP_KEY = "sabnzbd.path_map"
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
    url = get_setting(session, SAB_URL_KEY)
    key = api_key or os.environ.get(SAB_KEY_ENV, "")
    if not url or not key:
        return None
    return SabnzbdClient(url, key, transport=transport)


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
    if not names:
        return "NZB lists no files"
    exts = set()
    for name in names:
        m = re.search(r"\.([A-Za-z0-9]{2,5})$", name)
        ext = m.group(1).lower() if m else ""
        ext = "par2" if re.fullmatch(r"vol\d+\+\d+", ext) else ext
        if ext in FORMATS or any(f".{f}" in name.lower() for f in FORMATS):
            return None
        exts.add(ext)
    if exts and exts <= _NOT_MAGAZINE:
        return f"NZB holds no pdf/epub/cbz/cbr (only {', '.join(sorted(exts))})"
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
) -> BlockedRelease:
    row = BlockedRelease(
        guid=release.guid if release else None,
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
    prowlarr: ProwlarrClient,
    client: DownloadClient,
    today: date,
    check_limits: bool = True,
) -> GrabResult:
    """Send one release to the download client, after checking it again."""
    from periodicool.search import indexer_limits

    result = GrabResult(release)
    magazines = list(session.scalars(select(Magazine)))
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
    )
    if not ev.accepted:
        result.reasons = list(ev.reasons)
        return result
    mag = ev.magazine
    if active := _active_grab(session, mag.id, ev.issue_key):
        result.reasons.append(f"{ev.issue_key} is already downloading ({active.title})")
    category = get_setting(session, CATEGORY_KEY)
    if not category:
        result.reasons.append("no SABnzbd category set (config set sabnzbd.category NAME)")
    if not release.download_url:
        result.reasons.append("release has no download link")
    if check_limits and release.indexer_id is not None:
        for lim in indexer_limits(session, prowlarr):
            if lim.id == release.indexer_id and lim.grabs_left == 0:
                result.reasons.append(f"{lim.name} grab limit reached ({lim.grab_limit} today)")
    if result.reasons:
        return result

    try:
        nzb = prowlarr.fetch_nzb(release.download_url, release.indexer_id)
    except ProwlarrError as exc:
        result.reasons.append(f"NZB download failed: {exc}")
        return result
    if bad := check_nzb(nzb):
        blocklist(session, release, release.title, mag.id, ev.issue_key, bad)
        result.reasons.append(bad)
        return result
    try:
        job_id = client.add_nzb(nzb, release.title, category)
    except ClientError as exc:
        result.reasons.append(f"not sent to {client.name}: {exc}")
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
        client=client.name,
        job_id=job_id,
        category=category,
        status=GrabStatus.QUEUED,
        progress=0.0,
    )
    session.add(result.grab)
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


def _candidates(session: Session, mag_id: int | None = None, key: str | None = None):
    """Accepted releases per issue, best first."""
    query = select(Release).where(Release.accepted.is_(True))
    if mag_id is not None:
        query = query.where(Release.magazine_id == mag_id, Release.issue_key == key)
    by_issue: dict[tuple[int, str], list[Release]] = {}
    for rel in session.scalars(query):
        by_issue.setdefault((rel.magazine_id, rel.issue_key), []).append(rel)
    return {k: sorted(v, key=lambda r: r.rank) for k, v in by_issue.items()}


def _grab_first(session, releases, prowlarr, client, today) -> GrabResult | None:
    last = None
    for rel in releases[:MAX_TRIES_PER_ISSUE]:
        last = grab(session, rel, prowlarr, client, today)
        if last.ok:
            return last
    return last


def auto_grab(
    session: Session, prowlarr: ProwlarrClient | None, client: DownloadClient | None, today: date
) -> list[GrabResult]:
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


def monitor(
    session: Session,
    client: DownloadClient,
    settings: Settings,
    prowlarr: ProwlarrClient | None,
    today: date,
) -> MonitorResult:
    result = MonitorResult()
    active = list(
        session.scalars(
            select(Grab).where(Grab.client == client.name, Grab.status.in_(ACTIVE_GRAB))
        )
    )
    by_category: dict[str, list[Grab]] = {}
    for g in active:
        by_category.setdefault(g.category, []).append(g)
    for category, grabs in by_category.items():
        states = {s.job_id: s for s in client.jobs([g.job_id for g in grabs], category)}
        for g in grabs:
            state = states.get(g.job_id)
            if state is None:
                if g.status is GrabStatus.COMPLETED and g.storage_path:
                    _import(session, g, settings, prowlarr, client, today, result)
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
                reason = f"download failed: {state.fail_message or state.client_status}"
                if new := fail(session, g, reason, prowlarr, client, today):
                    result.retried.append(new)
                result.failed.append(g)
            elif state.status is JobStatus.COMPLETED:
                g.status, g.storage_path = GrabStatus.COMPLETED, state.storage_path
                g.completed_at = g.completed_at or datetime.now(UTC)
                _import(session, g, settings, prowlarr, client, today, result)
            elif state.status is JobStatus.DOWNLOADING:
                g.status = GrabStatus.DOWNLOADING
    session.flush()
    return result


def _import(session, g, settings, prowlarr, client, today, result) -> None:
    outcome = import_download(session, g, settings)
    if outcome == "imported":
        result.imported.append(g)
    elif outcome == "failed":
        reason = g.error or "import failed"
        if new := fail(
            session, g, reason, prowlarr, client, today, event=HistoryEvent.IMPORT_FAILED
        ):
            result.retried.append(new)
        result.failed.append(g)
    else:
        result.review.append(g)


def _issue_wanted(session: Session, g: Grab) -> None:
    issue = _issue(session, g.magazine_id, g.issue_key) if g.magazine_id else None
    if issue is not None and not issue.file_path:
        issue.status = IssueStatus.WANTED


def fail(
    session: Session,
    g: Grab,
    reason: str,
    prowlarr: ProwlarrClient | None,
    client: DownloadClient | None,
    today: date,
    event: HistoryEvent = HistoryEvent.DOWNLOAD_FAILED,
) -> Grab | None:
    """Blocklist the release, free the issue, and grab the next best release once."""
    blocklist(
        session, g.release, g.title, g.magazine_id, g.issue_key, reason, g.indexer_id, record=False
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


def import_download(session: Session, g: Grab, settings: Settings) -> str:
    """Move the finished file into the library. Returns imported, review or failed."""
    downloads = settings.downloads_dir.resolve()
    local = Path(map_path(g.storage_path or "", get_setting(session, PATH_MAP_KEY, []))).resolve()
    if local == downloads or not local.is_relative_to(downloads):
        why = f"download path {local} is outside {downloads}; check sabnzbd.path_map"
        return _review(session, g, why)
    if not local.exists():
        return _review(session, g, f"download path {local} not found; check sabnzbd.path_map")

    files = [local] if local.is_file() else walk(local)[0]
    files = [f for f in files if f.suffix.lower().lstrip(".") in FORMATS]
    if not files:
        g.error = "no pdf/epub/cbz/cbr in download"
        return "failed"
    mag = session.get(Magazine, g.magazine_id)
    issue = _issue(session, g.magazine_id, g.issue_key)
    if mag is None or issue is None:
        return _review(session, g, "magazine or issue no longer exists")

    def score(f: Path):
        parsed = parse_release(f.stem)
        ext = f.suffix.lower().lstrip(".")
        return [*format_rank(ext, parsed.true_pdf, mag.format_profile), -f.stat().st_size]

    best = min(files, key=score)
    parsed = parse_release(best.stem)
    if not parsed.reasons and match_title(parsed, mag.title_spec()).accepted:
        ident = identify(parsed, mag)
        if ident.confident and ident.key != g.issue_key:
            why = f"file {best.name} looks like {ident.key}, grabbed for {g.issue_key}"
            return _review(session, g, why)

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
    )
    log.info("Imported %s to %s", best, final)
    if get_setting(session, LEFTOVERS_KEY, True) and local.is_dir():
        if why := _safe_to_remove(local, downloads, library_root):
            log.warning("Leftovers kept at %s: %s", local, why)
        else:
            shutil.rmtree(local)
            log.info("Removed leftover folder of job %s: %s", g.job_id, local)
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


def remove_grab(
    session: Session, grab_id: int, client: DownloadClient, add_to_blocklist: bool = False
) -> Grab:
    """Remove one of our jobs from the download client (the queue's Remove button).

    Only grabs PeriodiCool made are touched, and the client checks the category again.
    """
    g = session.get(Grab, grab_id)
    if g is None:
        raise ValueError(f"no grab {grab_id}")
    if g.client != client.name:
        raise ValueError(f"grab {grab_id} belongs to {g.client}, not {client.name}")
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
    log.info("Removed %s (job %s) from %s", g.title, g.job_id, client.name)
    return g
