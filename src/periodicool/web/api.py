"""REST API under /api/v1, in Sonarr's v3 style: camelCase JSON, the API key in the
X-Api-Key header (or ?apikey=), paged lists as {page, pageSize, totalRecords, records},
and POST /command for the jobs.

Answers never carry indexer or client keys and passwords, webhook secrets, download
links or absolute paths; issue files are given relative to the library.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select

from periodicool import __version__, activity, auth, magazines
from periodicool.jobs import today_in
from periodicool.models import History, Issue, Magazine
from periodicool.overview import MISSING, issue_label, issue_state

PREFIX = "/api/v1"


class ApiError(Exception):
    def __init__(self, status: int, message: str, errors: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.errors = status, message, errors or {}


async def api_error(request: Request, exc: ApiError) -> JSONResponse:
    body = {"message": exc.message}
    if exc.errors:
        body["errors"] = exc.errors
    return JSONResponse(body, status_code=exc.status)


def require_key(request: Request) -> None:
    given = request.headers.get("x-api-key") or request.query_params.get("apikey")
    if not auth.api_key_matches(request.app.state.settings.config_dir, given):
        raise ApiError(401, "API key missing or wrong")


router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_key)], tags=["api"])


# Helpers


def camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(p.title() for p in rest)


def snake(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:  # SQLite hands back stored UTC times without a zone
            value = value.replace(tzinfo=UTC)
        return value.isoformat(timespec="seconds")
    return value.isoformat()


def _today(request: Request) -> date:
    return today_in(request.app.state.settings.tz)


def _paged(page, records) -> dict:
    return {
        "page": page.page,
        "pageSize": activity.PAGE_SIZE,
        "totalPages": page.pages,
        "totalRecords": page.total,
        "records": records,
    }


def _magazine_or_404(session, mag_id: int) -> Magazine:
    mag = session.get(Magazine, mag_id)
    if mag is None:
        raise ApiError(404, f"no magazine {mag_id}")
    return mag


# Shapes


def magazine_json(mag: Magazine, today: date) -> dict:
    out: dict = {"id": mag.id}
    for name in magazines.TEXT_FIELDS:
        out[camel(name)] = getattr(mag, name)
    for name in magazines.LIST_FIELDS:
        out[camel(name)] = list(getattr(mag, name) or [])
    for name in magazines.BOOL_FIELDS:
        out[camel(name)] = bool(getattr(mag, name))
    for name in magazines.INT_FIELDS:
        out[camel(name)] = getattr(mag, name)
    for name in magazines.DATE_FIELDS:
        out[camel(name)] = _iso(getattr(mag, name))
    issues = list(mag.issues)
    states = [issue_state(i, mag, today) for i in issues]
    return out | {
        "frequency": mag.frequency.value,
        "numbering": mag.numbering.value,
        "monitorMode": mag.monitor_mode.value,
        "formatProfileId": mag.format_profile_id,
        "added": _iso(mag.added_at),
        "statistics": {
            "issueCount": len(issues),
            "fileCount": sum(1 for i in issues if i.file_path),
            "missingCount": sum(1 for s in states if s == MISSING),
            "monitoredCount": sum(1 for i in issues if i.monitored),
        },
    }


def issue_json(issue: Issue, mag: Magazine, today: date) -> dict:
    return {
        "id": issue.id,
        "magazineId": issue.magazine_id,
        "key": issue.key,
        "label": issue_label(issue, mag),
        "coverDate": _iso(issue.cover_date),
        "year": issue.year,
        "month": issue.month,
        "season": issue.season,
        "issueNumber": issue.issue_number,
        "volumeNumber": issue.volume_number,
        "special": bool(issue.special),
        "monitored": bool(issue.monitored),
        "state": issue_state(issue, mag, today),
        "file": {
            "path": issue.file_path,  # relative to the library
            "format": issue.file_format,
            "size": issue.file_size,
            "imported": _iso(issue.imported_at),
        }
        if issue.file_path
        else None,
    }


def grab_json(g) -> dict:
    return {
        "id": g.id,
        "magazineId": g.magazine_id,
        "issueKey": g.issue_key,
        "title": g.title,
        "status": g.status.value,
        "protocol": g.protocol,
        "downloadClient": g.client_label,
        "downloadId": g.job_id,
        "progress": g.progress,
        "size": g.size_bytes,
        "sizeLeft": g.size_left_bytes,
        "timeLeft": g.time_left_s,
        "error": g.error,
        "grabbed": _iso(g.grabbed_at),
    }


def history_json(row: History) -> dict:
    data = {k: v for k, v in (row.data or {}).items() if k in ("reason", "client", "size")}
    return {
        "id": row.id,
        "date": _iso(row.date),
        "eventType": row.event.value,
        "magazineId": row.magazine_id,
        "issueKey": row.issue_key,
        "sourceTitle": row.source_title,
        "data": data,
    }


# Magazine input: camelCase JSON in, the Add/Edit form's own parsing and checks.

ALLOWED_INPUT = {
    *magazines.TEXT_FIELDS,
    *magazines.LIST_FIELDS,
    *magazines.BOOL_FIELDS,
    *magazines.INT_FIELDS,
    *magazines.DATE_FIELDS,
    "frequency",
    "numbering",
    "monitor_mode",
    "format_profile_id",
}


def _form_from_json(body: dict, base: dict) -> dict[str, str]:
    if not isinstance(body, dict):
        raise ApiError(400, "send a JSON object")
    given = {snake(k): v for k, v in body.items() if k not in ("id", "statistics", "added")}
    if unknown := sorted(set(given) - ALLOWED_INPUT):
        raise ApiError(400, f"unknown fields: {', '.join(camel(u) for u in unknown)}")
    merged = {**base, **given}
    form: dict[str, str] = {}
    for name, value in merged.items():
        if name in magazines.BOOL_FIELDS:
            form[name] = "on" if value is True or value == "on" else ""
        elif name in magazines.LIST_FIELDS and isinstance(value, list):
            form[name] = ", ".join(str(v) for v in value)
        else:
            form[name] = "" if value is None else str(value)
    return form


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except ValueError as exc:
        raise ApiError(400, "the body is not valid JSON") from exc


def _title_taken(session, title: str, mag_id: int | None = None) -> bool:
    query = select(Magazine.id).where(func.lower(Magazine.title) == title.strip().lower())
    if mag_id is not None:
        query = query.where(Magazine.id != mag_id)
    return session.scalar(query) is not None


# System


@router.get("/system/status")
def system_status(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "appName": "PeriodiCool",
        "version": __version__,
        "startTime": _iso(request.app.state.started_at),
        "urlBase": request.scope.get("root_path", ""),
        "timeZone": settings.tz,
        "jobsEnabled": settings.jobs_enabled,
    }


@router.get("/health")
def health_checks(request: Request) -> list[dict]:
    from datetime import UTC

    from periodicool import health

    now = datetime.now(UTC)
    with request.app.state.sessions() as session:
        checks = health.local_checks(session, request.app.state.settings, now)
        checks += health.source_checks(session, now)
    return [{"name": c.name, "level": c.kind, "message": c.message, "fix": c.fix} for c in checks]


# Magazines


@router.get("/magazine")
def magazine_list(request: Request) -> list[dict]:
    today = _today(request)
    with request.app.state.sessions() as session:
        mags = session.scalars(select(Magazine).order_by(func.lower(Magazine.title)))
        return [magazine_json(m, today) for m in mags]


@router.get("/magazine/{mag_id:int}")
def magazine_one(request: Request, mag_id: int) -> dict:
    with request.app.state.sessions() as session:
        return magazine_json(_magazine_or_404(session, mag_id), _today(request))


@router.post("/magazine", status_code=201)
async def magazine_add(request: Request) -> dict:
    from periodicool import notify
    from periodicool.schedule import sync_expected_issues

    body = await _json(request)
    today = _today(request)
    with request.app.state.sessions() as session:
        defaults = magazines.form_values(None)
        form = _form_from_json(body, defaults)
        if _title_taken(session, form.get("title", "")):
            raise ApiError(409, "a magazine with this title already exists")
        parsed = magazines.parse_form(session, form)
        if not parsed.ok:
            raise ApiError(400, "check the fields", {camel(k): v for k, v in parsed.errors.items()})
        mag = magazines.apply_values(Magazine(issues=[]), parsed.values)
        session.add(mag)
        session.flush()
        notify.magazine_event(session, "MagazineAdd", mag)
        sync_expected_issues(session, mag, today)
        session.commit()
        session.refresh(mag)  # load the issues just generated
        return magazine_json(mag, today)


@router.put("/magazine/{mag_id:int}")
async def magazine_edit(request: Request, mag_id: int) -> dict:
    from periodicool.schedule import sync_expected_issues

    body = await _json(request)
    today = _today(request)
    with request.app.state.sessions() as session:
        mag = _magazine_or_404(session, mag_id)
        form = _form_from_json(body, magazines.form_values(mag))
        if _title_taken(session, form.get("title", ""), mag.id):
            raise ApiError(409, "a magazine with this title already exists")
        parsed = magazines.parse_form(session, form)
        if not parsed.ok:
            raise ApiError(400, "check the fields", {camel(k): v for k, v in parsed.errors.items()})
        magazines.apply_values(mag, parsed.values)
        sync_expected_issues(session, mag, today)
        session.commit()
        session.refresh(mag)  # load the issues just generated
        return magazine_json(mag, today)


@router.delete("/magazine/{mag_id:int}")
def magazine_delete(request: Request, mag_id: int) -> Response:
    """Removes the magazine from PeriodiCool. Its files stay on disk."""
    with request.app.state.sessions() as session:
        _magazine_or_404(session, mag_id)
        magazines.delete_magazines(session, [mag_id])
        session.commit()
    return Response(status_code=200)


# Issues


@router.get("/issue")
def issue_list(request: Request, magazineId: int) -> list[dict]:  # noqa: N803  Sonarr's name
    today = _today(request)
    with request.app.state.sessions() as session:
        mag = _magazine_or_404(session, magazineId)
        issues = sorted(mag.issues, key=lambda i: (i.cover_date or date.min, i.key))
        return [issue_json(i, mag, today) for i in issues]


@router.put("/issue/{issue_id:int}")
async def issue_edit(request: Request, issue_id: int) -> dict:
    body = await _json(request)
    if set(body) - {"monitored", "id"} or not isinstance(body.get("monitored"), bool):
        raise ApiError(400, 'only {"monitored": true or false} can be changed')
    with request.app.state.sessions() as session:
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise ApiError(404, f"no issue {issue_id}")
        issue.monitored = body["monitored"]
        session.commit()
        return issue_json(issue, issue.magazine, _today(request))


# Wanted, queue, history, calendar


@router.get("/wanted/missing")
def wanted_missing(request: Request, page: int = 1, magazineId: int | None = None) -> dict:  # noqa: N803
    today = _today(request)
    with request.app.state.sessions() as session:
        result = activity.missing(session, today, page, magazine_id=magazineId)
        records = [
            issue_json(r.issue, r.mag, today) | {"magazineTitle": r.mag.title} for r in result.rows
        ]
        return _paged(result, records)


@router.get("/queue")
def queue_list(request: Request) -> dict:
    with request.app.state.sessions() as session:
        rows = [grab_json(g) for g in activity.queue(session)]
    return {"totalRecords": len(rows), "records": rows}


@router.get("/history")
def history_list(
    request: Request,
    page: int = 1,
    magazineId: int | None = None,  # noqa: N803
    eventType: str | None = None,  # noqa: N803
) -> dict:
    from periodicool.models import HistoryEvent

    if eventType and eventType not in {e.value for e in HistoryEvent}:
        raise ApiError(
            400, f"unknown eventType; use one of {', '.join(e.value for e in HistoryEvent)}"
        )
    with request.app.state.sessions() as session:
        result = activity.history_page(session, page, eventType, magazineId)
        return _paged(result, [history_json(r) for r in result.rows])


@router.get("/calendar")
def calendar(request: Request, start: date | None = None, end: date | None = None) -> list[dict]:
    today = _today(request)
    start = start or today - timedelta(days=7)
    end = end or start + timedelta(days=35)
    if end < start or (end - start).days > 366:
        raise ApiError(400, "end must be after start, at most a year apart")
    out = []
    with request.app.state.sessions() as session:
        for day, entries in activity.entries(session, start, end, today).items():
            for entry in entries:
                out.append(
                    issue_json(entry.issue, entry.mag, today)
                    | {
                        "magazineTitle": entry.mag.title,
                        "date": day.isoformat(),
                        "onSale": _iso(entry.on_sale),
                    }
                )
    return out


# Commands


def _commands(request: Request):
    commands = getattr(request.app.state, "api_commands", None)
    if commands is None:
        raise ApiError(503, "commands are not available right now")
    return commands


@router.post("/command", status_code=201)
async def command_start(request: Request) -> dict:
    from periodicool import api_commands

    body = await _json(request)
    name = body.get("name") if isinstance(body, dict) else None
    if name not in api_commands.NAMES:
        raise ApiError(400, f"unknown command; use one of {', '.join(api_commands.NAMES)}")
    magazine_id = body.get("magazineId")
    if name == "MagazineSearch":
        if not isinstance(magazine_id, int):
            raise ApiError(400, "MagazineSearch needs magazineId")
        with request.app.state.sessions() as session:
            _magazine_or_404(session, magazine_id)
    return _commands(request).submit(name, magazine_id).as_json()


@router.get("/command")
def command_list(request: Request) -> list[dict]:
    return [c.as_json() for c in _commands(request).recent()]


@router.get("/command/{command_id:int}")
def command_one(request: Request, command_id: int) -> dict:
    cmd = _commands(request).get(command_id)
    if cmd is None:
        raise ApiError(404, f"no command {command_id} (only the last 50 are kept)")
    return cmd.as_json()


@router.get("/openapi.json", include_in_schema=False)
def openapi(request: Request) -> dict:
    return request.app.openapi()


def start_commands(app) -> None:
    """The command worker, using the app's sessions and job runners."""
    from periodicool import api_commands
    from periodicool.jobs import RUN_LOCK, Scheduler
    from periodicool.sources import Sources, transports_from

    state = app.state

    def scheduler():
        return Scheduler(
            state.sessions, state.settings, runners=getattr(state, "job_runners", None)
        )

    def search(magazine_id: int) -> str:
        with RUN_LOCK, state.sessions() as session:
            mag = session.get(Magazine, magazine_id)
            if mag is None:
                return "error: the magazine was removed"
            sources = Sources(session, transports_from(state))
            if not sources.indexers:
                return "error: no indexer is set up"
            result = magazines.automatic_search(
                session,
                mag,
                today_in(state.settings.tz),
                sources,
                sources if sources.clients else None,
            )
            session.commit()
        parts = [f"{result.found} accepted releases", f"{len(result.grabbed)} grabbed"]
        return "; ".join(parts + result.problems)

    state.api_commands = api_commands.Commands(scheduler, search)
