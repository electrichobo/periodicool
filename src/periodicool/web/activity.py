"""Activity (queue, history, blocklist), Wanted (missing, cutoff unmet), Calendar and iCal."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from periodicool import activity, auth, downloads, magazines
from periodicool.models import BlockedRelease, Grab, GrabStatus, History, Issue, Magazine
from periodicool.overview import EVENT_LABELS
from periodicool.prowlarr import ProwlarrError, RateLimited
from periodicool.web.magazines import _render, _toast, _today, prowlarr_client, sab_client
from periodicool.web.security import form_data, form_lists

router = APIRouter(include_in_schema=False)

WEEKDAYS = [
    ("Mon", "Monday"),
    ("Tue", "Tuesday"),
    ("Wed", "Wednesday"),
    ("Thu", "Thursday"),
    ("Fri", "Friday"),
    ("Sat", "Saturday"),
    ("Sun", "Sunday"),
]


def _ids(form: dict[str, list[str]]) -> list[int]:
    return [int(i) for i in form.get("ids", []) if i.isdigit()]


# Queue


@router.get("/activity")
def activity_home() -> RedirectResponse:
    return RedirectResponse("/activity/queue", status_code=307)


@router.get("/history")
def history_home() -> RedirectResponse:
    return RedirectResponse("/activity/history", status_code=307)


def _queue_context(session) -> dict:
    return {
        "grabs": activity.queue(session),
        "downloading": activity.has_active(session),
        "leds": activity.GRAB_LEDS,
        "fmt_seconds": activity.format_seconds,
    }


@router.get("/activity/queue", response_class=HTMLResponse)
def queue_page(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        context = _queue_context(session)
        return _render(request, "activity/queue.html", active="/activity", tab="queue", **context)


@router.get("/activity/queue/table", response_class=HTMLResponse)
def queue_table(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        return _render(request, "activity/_queue_table.html", **_queue_context(session))


@router.post("/tasks/downloads")
def downloads_now(request: Request) -> Response:
    settings = request.app.state.settings
    with request.app.state.sessions() as session:
        client = sab_client(request, session)
        if client is None:
            return _toast("SABnzbd is not set up yet (Settings).", "warn")
        r = downloads.monitor(
            session, client, settings, prowlarr_client(request, session), _today(request)
        )
        session.commit()
    text = f"{r.updated} active, {len(r.imported)} imported, {len(r.failed)} failed"
    return _toast(f"Checked downloads: {text}.", refreshQueue=True)


@router.get("/activity/queue/{grab_id:int}/remove", response_class=HTMLResponse)
def remove_modal(request: Request, grab_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        g = session.get(Grab, grab_id)
        if g is None:
            raise HTTPException(status_code=404)
        return _render(request, "activity/_remove.html", g=g)


@router.post("/activity/queue/{grab_id:int}/remove")
async def remove_submit(request: Request, grab_id: int) -> Response:
    form = await form_data(request)
    with request.app.state.sessions() as session:
        client = sab_client(request, session)
        if client is None:
            return _toast("SABnzbd is not set up yet (Settings).", "warn")
        try:
            g = downloads.remove_grab(
                session, grab_id, client, add_to_blocklist=form.get("blocklist") == "on"
            )
        except ValueError as exc:
            return _toast(str(exc), "bad")
        session.commit()
    return _toast(f"Removed {g.title}.", closeModal=True, refreshQueue=True)


# History


@router.get("/activity/history", response_class=HTMLResponse)
def history_page(request: Request, page: int = 1, event: str = "") -> HTMLResponse:
    event = event if event in EVENT_LABELS else ""
    with request.app.state.sessions() as session:
        result = activity.history_page(session, page, event or None)
        grab_ids = [h.grab_id for h in result.rows if h.grab_id]
        grabs = {g.id: g for g in session.scalars(select(Grab).where(Grab.id.in_(grab_ids)))}
        return _render(
            request,
            "activity/history.html",
            active="/activity",
            tab="history",
            result=result,
            event=event,
            grabs=grabs,
            pager_url=f"?event={event}&" if event else "?",
        )


@router.get("/history/{history_id:int}", response_class=HTMLResponse)
def history_detail(request: Request, history_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        h = session.get(History, history_id, options=[selectinload(History.magazine)])
        if h is None:
            raise HTTPException(status_code=404)
        g = session.get(Grab, h.grab_id) if h.grab_id else None
        return _render(request, "activity/_history_detail.html", h=h, g=g)


@router.post("/history/{history_id:int}/fail")
def mark_failed(request: Request, history_id: int) -> Response:
    """Blocklist the release of an imported grab. The file stays where it is."""
    with request.app.state.sessions() as session:
        h = session.get(History, history_id)
        g = session.get(Grab, h.grab_id) if h and h.grab_id else None
        if g is None or g.status is not GrabStatus.IMPORTED:
            return _toast("Only imported downloads can be marked as failed.", "warn")
        downloads.fail(session, g, "marked as failed by the user", None, None, _today(request))
        session.commit()
    return _toast(
        "Marked as failed and blocklisted. The file is still in the library; "
        "unlink it on the issue to look for a replacement.",
        closeModal=True,
    )


# Blocklist


@router.get("/activity/blocklist", response_class=HTMLResponse)
def blocklist_page(request: Request, page: int = 1) -> HTMLResponse:
    with request.app.state.sessions() as session:
        total = session.scalar(select(func.count()).select_from(BlockedRelease)) or 0
        page, pages, offset = activity._page(total, page)
        rows = list(
            session.scalars(
                select(BlockedRelease)
                .order_by(BlockedRelease.created_at.desc())
                .offset(offset)
                .limit(activity.PAGE_SIZE)
            )
        )
        titles = dict(session.execute(select(Magazine.id, Magazine.title)).all())
        return _render(
            request,
            "activity/blocklist.html",
            active="/activity",
            tab="blocklist",
            rows=rows,
            titles=titles,
            page=page,
            pages=pages,
            total=total,
        )


@router.post("/activity/blocklist/remove")
async def blocklist_remove(request: Request) -> Response:
    ids = _ids(await form_lists(request))
    if not ids:
        return _toast("Select at least one release.", "warn")
    with request.app.state.sessions() as session:
        session.execute(delete(BlockedRelease).where(BlockedRelease.id.in_(ids)))
        session.commit()
    response = _toast(f"Removed {len(ids)} from the blocklist.")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/activity/blocklist/clear")
def blocklist_clear(request: Request) -> Response:
    with request.app.state.sessions() as session:
        count = session.execute(delete(BlockedRelease)).rowcount
        session.commit()
    response = _toast(f"Cleared the blocklist ({count} releases).")
    response.headers["HX-Refresh"] = "true"
    return response


# Wanted


@router.get("/wanted")
def wanted_home() -> RedirectResponse:
    return RedirectResponse("/wanted/missing", status_code=307)


@router.get("/wanted/missing", response_class=HTMLResponse)
def wanted_missing(request: Request, page: int = 1) -> HTMLResponse:
    with request.app.state.sessions() as session:
        result = activity.missing(session, _today(request), page)
        return _render(
            request, "wanted/missing.html", active="/wanted", tab="missing", result=result
        )


@router.get("/wanted/cutoff", response_class=HTMLResponse)
def wanted_cutoff(request: Request, page: int = 1) -> HTMLResponse:
    with request.app.state.sessions() as session:
        result = activity.cutoff_unmet(session, page)
        return _render(request, "wanted/cutoff.html", active="/wanted", tab="cutoff", result=result)


@router.post("/wanted/search")
async def wanted_search_selected(request: Request) -> Response:
    """Automatic search for the selected issues: one search per magazine, five at most."""
    ids = _ids(await form_lists(request))
    with request.app.state.sessions() as session:
        issues = list(session.scalars(select(Issue).where(Issue.id.in_(ids))))
        if not issues:
            return _toast("Select at least one issue.", "warn")
        client = prowlarr_client(request, session)
        if client is None:
            return _toast("Prowlarr is not set up yet (Settings).", "warn")
        by_mag: dict[int, list[Issue]] = {}
        for issue in issues:
            by_mag.setdefault(issue.magazine_id, []).append(issue)
        sab = sab_client(request, session)
        grabbed, skipped = [], max(0, len(by_mag) - 5)
        try:
            for mag_issues in list(by_mag.values())[:5]:
                mag = mag_issues[0].magazine
                for issue in mag_issues:
                    res = magazines.automatic_search(
                        session, mag, _today(request), client, sab, issue.key
                    )
                    grabbed += [f"{mag.title} {k}" for k in res.grabbed]
        except (ProwlarrError, RateLimited) as exc:
            session.commit()
            return _toast(f"Search stopped: {exc}", "bad")
        session.commit()
    text = f"Grabbed {len(grabbed)}." if grabbed else "Nothing grabbed."
    if skipped:
        text += f" {skipped} more magazines left for the next run, to spare indexer limits."
    return _toast(text, "ok" if grabbed else "warn")


@router.post("/wanted/unmonitor")
async def wanted_unmonitor(request: Request) -> Response:
    ids = _ids(await form_lists(request))
    with request.app.state.sessions() as session:
        for issue in session.scalars(select(Issue).where(Issue.id.in_(ids))):
            issue.monitored = False
        session.commit()
    response = _toast(f"Stopped monitoring {len(ids)} issues.")
    response.headers["HX-Refresh"] = "true"
    return response


# Calendar


def _parse_day(value: str, today: date) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return today


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, view: str = "month", day: str = "") -> HTMLResponse:
    view = view if view in ("month", "week", "agenda") else "month"
    today = _today(request)
    focus = _parse_day(day, today) if day else today
    with request.app.state.sessions() as session:
        from periodicool.library import get_setting

        first_weekday = 6 if str(get_setting(session, "ui.first_weekday", "0")) == "6" else 0
    if view == "month":
        weeks = activity.month_grid(focus.year, focus.month, first_weekday)
        start, end = weeks[0][0], weeks[-1][-1]
        prev_day = (focus.replace(day=1) - timedelta(days=1)).replace(day=1)
        next_day = (focus.replace(day=28) + timedelta(days=5)).replace(day=1)
        title = focus.strftime("%B %Y")
    elif view == "week":
        days = activity.week_days(focus, first_weekday)
        weeks = [days]
        start, end = days[0], days[-1]
        prev_day, next_day = focus - timedelta(days=7), focus + timedelta(days=7)
        title = f"{start.isoformat()} to {end.isoformat()}"
    else:
        weeks = []
        start, end = focus, focus + timedelta(days=60)
        prev_day, next_day = focus - timedelta(days=60), focus + timedelta(days=60)
        title = f"{start.isoformat()} to {end.isoformat()}"
    with request.app.state.sessions() as session:
        by_day = activity.entries(session, start, end, today)
        return _render(
            request,
            "calendar.html",
            active="/calendar",
            view=view,
            weeks=weeks,
            by_day=by_day,
            focus=focus,
            today=today,
            title=title,
            prev_day=prev_day,
            next_day=next_day,
            weekday_names=WEEKDAYS[first_weekday:] + WEEKDAYS[:first_weekday],
            api_key=auth.api_key(request.app.state.settings.config_dir),
        )


@router.get("/feed/calendar.ics")
def calendar_feed(request: Request, apikey: str = "") -> Response:
    given = apikey or request.headers.get("x-api-key")
    if not auth.api_key_matches(request.app.state.settings.config_dir, given):
        return Response("API key missing or wrong", status_code=401)
    with request.app.state.sessions() as session:
        text = activity.ical_feed(session, _today(request))
    return Response(
        text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="periodicool.ics"'},
    )
