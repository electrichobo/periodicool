"""System (status, health, tasks, backups, events, log files) and Library (scan, manual import)."""

from __future__ import annotations

import platform
import sqlite3
import sys
from collections import deque
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from periodicool import __version__, auth, backup, health, library
from periodicool.config import DB_FILENAME
from periodicool.jobs import DEFAULT_INTERVALS, LABELS, Scheduler
from periodicool.logs import LOG_DIR
from periodicool.models import ImportItem, ItemAction, ItemStatus, Job, LogEvent, Magazine, ScanKind
from periodicool.web.magazines import _render, _toast
from periodicool.web.security import form_data
from periodicool.web.settings import _paths

router = APIRouter(include_in_schema=False)

SYSTEM_TABS = (
    ("status", "Status"),
    ("health", "Health"),
    ("tasks", "Tasks"),
    ("backups", "Backups"),
    ("events", "Events"),
    ("logs", "Log files"),
)
LEVELS = ("INFO", "WARNING", "ERROR")


def _system(request, page, tab, /, **context):
    return _render(
        request, f"system/{page}.html", active="/system", tabs=SYSTEM_TABS, tab=tab, **context
    )


@router.get("/system")
def system_home() -> RedirectResponse:
    return RedirectResponse("/system/status", status_code=307)


# Status


@router.get("/system/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    state, settings = request.app.state, request.app.state.settings
    started = getattr(state, "started_at", None)
    now = datetime.now(UTC)
    db_file = settings.config_dir / DB_FILENAME
    with state.sessions() as session:
        counts = {
            "magazines": session.scalar(select(func.count()).select_from(Magazine)) or 0,
        }
    return _system(
        request,
        "status",
        "status",
        version=__version__,
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
        os_name=f"{platform.system()} {platform.release()}",
        started=started,
        uptime=str(now - started).split(".")[0] if started else "",
        db_size=db_file.stat().st_size if db_file.exists() else 0,
        config_dir=str(settings.config_dir),
        paths=_paths(settings),
        tz=settings.tz,
        counts=counts,
        argv=" ".join(sys.argv[:1]),
    )


# Health


@router.get("/system/health", response_class=HTMLResponse)
def health_page(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        checks = health.local_checks(session, request.app.state.settings)
    return _system(request, "health", "health", checks=checks)


@router.post("/system/health/connections", response_class=HTMLResponse)
def health_connections(request: Request) -> HTMLResponse:
    from periodicool.web.magazines import prowlarr_client, sab_client

    checks = []
    with request.app.state.sessions() as session:
        p_url = library.get_setting(session, "prowlarr.url")
        s_url = library.get_setting(session, "sabnzbd.url")
        prowlarr = prowlarr_client(request, session)
        if prowlarr is not None:
            try:
                from periodicool.search import indexer_limits

                limits = indexer_limits(session, prowlarr)
                session.commit()
                checks.append(
                    health.Check("Prowlarr", "ok", f"Answered; {len(limits)} usable indexers.")
                )
                for lim in limits:
                    if why := lim.unavailable(datetime.now(UTC)):
                        checks.append(health.Check(f"Indexer {lim.name}", "warn", why))
            except Exception as exc:  # noqa: BLE001  report any failure as a check
                checks.append(
                    health.Check("Prowlarr", "bad", str(exc)[:300], "Settings, Indexers.")
                )
        sab = sab_client(request, session)
        if sab is not None:
            try:
                version = sab.check(library.get_setting(session, "sabnzbd.category", ""))
                checks.append(health.Check("SABnzbd", "ok", f"Version {version}; category exists."))
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    health.Check("SABnzbd", "bad", str(exc)[:300], "Settings, Download clients.")
                )
    fetch = getattr(request.app.state, "clock_fetch", health.remote_date)
    checks.append(health.clock_check("Prowlarr", p_url, fetch=fetch))
    checks.append(health.clock_check("SABnzbd", s_url, fetch=fetch))
    return _render(request, "system/_checks.html", checks=checks)


# Tasks


@router.get("/system/tasks", response_class=HTMLResponse)
def tasks(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        jobs = {j.name: j for j in session.scalars(select(Job))}
    rows = []
    for name in DEFAULT_INTERVALS:
        job = jobs.get(name)
        duration = None
        if job and job.last_started_at and job.last_finished_at:
            duration = (job.last_finished_at - job.last_started_at).total_seconds()
        rows.append(
            {
                "name": name,
                "label": LABELS[name],
                "job": job,
                "interval": job.interval_s if job else DEFAULT_INTERVALS[name],
                "duration": duration,
            }
        )
    return _system(
        request, "tasks", "tasks", rows=rows, enabled=request.app.state.settings.jobs_enabled
    )


@router.post("/system/tasks/{name}/run")
def run_task(request: Request, name: str) -> Response:
    if name not in DEFAULT_INTERVALS:
        raise HTTPException(status_code=404)
    runners = getattr(request.app.state, "job_runners", None)
    scheduler = Scheduler(request.app.state.sessions, request.app.state.settings, runners=runners)
    result = scheduler.run_now(name)
    if result is None:
        return _toast("Another task is running. Try again in a moment.", "warn")
    response = _toast(f"{LABELS[name]}: {result}", "bad" if result.startswith("error") else "ok")
    response.headers["HX-Refresh"] = "true"
    return response


# Backups


@router.get("/system/backups", response_class=HTMLResponse)
def backups(request: Request) -> HTMLResponse:
    config_dir = request.app.state.settings.config_dir
    return _system(
        request,
        "backups",
        "backups",
        files=backup.list_backups(config_dir),
        folder=str(backup.backup_dir(config_dir)),
    )


@router.post("/system/backups")
def backup_now(request: Request) -> Response:
    settings = request.app.state.settings
    with request.app.state.sessions() as session:
        keep = int(library.get_setting(session, "backup.keep", 7))
    made = backup.create_backup(settings.config_dir, keep=keep)
    response = _toast(f"Backup made: {made.name}")
    response.headers["HX-Refresh"] = "true"
    return response


@router.get("/system/backups/{name}")
def backup_download(request: Request, name: str) -> Response:
    try:
        path = backup.backup_path(request.app.state.settings.config_dir, name)
    except backup.BackupError as exc:
        raise HTTPException(status_code=404) from exc
    return FileResponse(path, media_type="application/zip", filename=name)


@router.post("/system/backups/{name}/restore")
def backup_restore(request: Request, name: str) -> Response:
    state = request.app.state
    try:
        safety = backup.restore_backup(state.settings.config_dir, name, state.engine)
    except backup.BackupError as exc:
        return _toast(f"Not restored: {exc}", "bad")
    state.secret = auth.load_secret(state.settings.config_dir)
    response = _toast(f"Restored {name}. The previous database is saved as {safety}. Log in again.")
    response.headers["HX-Refresh"] = "true"
    return response


# Events and log files


@router.get("/system/events", response_class=HTMLResponse)
def events(request: Request, level: str = "", page: int = 1) -> HTMLResponse:
    from periodicool.activity import PAGE_SIZE, _page

    level = level.upper() if level.upper() in LEVELS else ""
    with request.app.state.sessions() as session:
        query = select(LogEvent)
        if level:
            wanted = LEVELS[LEVELS.index(level) :]
            query = query.where(LogEvent.level.in_(wanted))
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        page, pages, offset = _page(total, page)
        rows = list(
            session.scalars(query.order_by(LogEvent.id.desc()).offset(offset).limit(PAGE_SIZE))
        )
    return _system(
        request,
        "events",
        "events",
        rows=rows,
        level=level,
        levels=LEVELS,
        page=page,
        pages=pages,
        total=total,
        pager_url=f"?level={level}&" if level else "?",
    )


def _log_files(request) -> list:
    folder = request.app.state.settings.config_dir / LOG_DIR
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("periodicool.txt")]
    return sorted(files, key=lambda p: p.name)


def _log_file(request, name: str):
    for path in _log_files(request):
        if path.name == name:
            return path
    raise HTTPException(status_code=404)


@router.get("/system/logs", response_class=HTMLResponse)
def log_files(request: Request) -> HTMLResponse:
    files = [
        (p.name, p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime, UTC))
        for p in _log_files(request)
    ]
    return _system(request, "logs", "logs", files=files)


@router.get("/system/logs/{name}", response_class=HTMLResponse)
def log_view(request: Request, name: str, lines: int = 300) -> HTMLResponse:
    path = _log_file(request, name)
    with path.open(encoding="utf-8", errors="replace") as fh:
        tail = list(deque(fh, maxlen=max(10, min(lines, 5000))))
    return _system(request, "log_view", "logs", name=name, tail="".join(tail), lines=lines)


@router.get("/system/logs/{name}/download")
def log_download(request: Request, name: str) -> Response:
    return FileResponse(_log_file(request, name), media_type="text/plain", filename=name)


# Library: scan preview and manual import


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request, scan: int = 0) -> HTMLResponse:
    from periodicool.models import Scan

    settings = request.app.state.settings
    with request.app.state.sessions() as session:
        current = session.get(Scan, scan) if scan else None
        items = []
        if current is not None:
            items = list(
                session.scalars(
                    select(ImportItem)
                    .where(ImportItem.scan_id == current.id)
                    .where(ImportItem.action != ItemAction.KEEP)
                    .order_by(ImportItem.id)
                )
            )
        waiting = library.queue(session)
        magazines = list(session.scalars(select(Magazine).order_by(Magazine.title)))
        titles = {m.id: m.title for m in magazines}
        return _render(
            request,
            "library.html",
            active="/library",
            scan=current,
            items=items,
            waiting=waiting,
            magazines=magazines,
            titles=titles,
            library=settings.library_dir,
            first_done=library.get_setting(session, library.FIRST_SCAN_KEY, False),
        )


@router.post("/library/scan")
def library_scan(request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.library_dir.is_dir():
        return _toast(f"The library folder {settings.library_dir} does not exist.", "bad")
    with request.app.state.sessions() as session:
        library.refresh_file_info(session, settings.library_dir)
        scan = library.plan_scan(session, settings.library_dir, ScanKind.LIBRARY)
        session.commit()
        scan_id = scan.id
    return Response(status_code=204, headers={"HX-Redirect": f"/library?scan={scan_id}"})


@router.post("/library/queue/{item_id:int}/resolve")
async def queue_resolve(request: Request, item_id: int) -> Response:
    form = await form_data(request)
    settings = request.app.state.settings
    mag_id, key = form.get("magazine_id", ""), form.get("key", "").strip()
    if not mag_id.isdigit() or not key:
        return _toast("Pick a magazine and enter the issue, for example 2026-10 or n475.", "warn")
    with request.app.state.sessions() as session:
        try:
            item = library.resolve(session, item_id, int(mag_id), key, settings.library_dir)
        except (ValueError, FileExistsError) as exc:
            session.rollback()
            return _toast(f"Not matched: {exc}", "bad")
        result = {"moved": 0, "kept": 0, "failed": 0, "review": 0}
        library.apply_item(session, item, settings.library_dir.resolve(), result)
        session.commit()
        done = item.status is ItemStatus.DONE
        target = item.target_path
    response = _toast(
        f"Moved to {target}." if done else f"Not moved: {item.reason}", "ok" if done else "bad"
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/library/queue/{item_id:int}/skip")
def queue_skip(request: Request, item_id: int) -> Response:
    with request.app.state.sessions() as session:
        if session.get(ImportItem, item_id) is None:
            raise HTTPException(status_code=404)
        library.skip(session, item_id)
        session.commit()
    response = _toast("Skipped. The file stays where it is.")
    response.headers["HX-Refresh"] = "true"
    return response
