"""Metadata pop-up and lookups. Every source is off until the user turns it on."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from periodicool.issn import normalize
from periodicool.library import set_setting
from periodicool.lookups import NOTICE_KEY, SOURCES, enabled
from periodicool.lookups import service as lookups
from periodicool.lookups.archive import back_issues
from periodicool.models import Frequency, Issue, IssueStatus, Magazine, MonitorMode, Numbering
from periodicool.overview import FREQUENCY_LABELS
from periodicool.web.magazines import _magazine, _render, _toast
from periodicool.web.security import form_data, form_lists

router = APIRouter(include_in_schema=False)

ISSUE_FIELDS = (
    "year",
    "month",
    "month_end",
    "day",
    "season",
    "issue_number",
    "volume_number",
    "special",
)


def _transport(request: Request):
    return getattr(request.app.state, "lookup_transport", None)


def _states(session) -> dict[str, bool]:
    return {s: enabled(session, s) for s in SOURCES}


def _back_issues(session, request, mag):
    """Back-issue suggestions from the Internet Archive, or None when not wanted."""
    if mag.monitor_mode not in (MonitorMode.ALL, MonitorMode.FROM_YEAR):
        return None
    answer = lookups.lookup(session, "archive", title=mag.title, transport=_transport(request))
    if answer.off or answer.error or not answer.data:
        return {"error": answer.error, "off": answer.off, "issues": [], "skipped": 0}
    found, skipped = back_issues(mag, answer.data.get("items", []))
    have = {i.key for i in mag.issues}
    start = mag.monitor_from_year if mag.monitor_mode is MonitorMode.FROM_YEAR else None
    fresh = [
        b
        for b in found
        if b.key not in have and (start is None or (b.fields.get("year") or 0) >= start)
    ]
    return {"error": None, "off": False, "issues": fresh, "skipped": skipped}


@router.get("/magazines/{mag_id:int}/metadata", response_class=HTMLResponse)
def metadata_popup(request: Request, mag_id: int, refresh: bool = False) -> HTMLResponse:
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        states = _states(session)
        answers = (
            lookups.records(session, mag, transport=_transport(request), force=refresh)
            if states["loc"] or states["wikidata"]
            else {}
        )
        back = _back_issues(session, request, mag) if states["archive"] else None
        session.commit()
        records = {s: a.record for s, a in answers.items()}
        errors = {s: a.error for s, a in answers.items() if a.error}
        return _render(
            request,
            "magazines/_metadata.html",
            mag=mag,
            states=states,
            sources=SOURCES,
            records=records,
            errors=errors,
            back=back,
            frequency_labels=FREQUENCY_LABELS,
            numbered=mag.numbering in (Numbering.ISSUE, Numbering.VOLUME),
        )


@router.post("/magazines/{mag_id:int}/metadata/apply")
async def metadata_apply(request: Request, mag_id: int) -> Response:
    """Apply only what the user ticked. Aliases are added, never replaced."""
    picks = (await form_lists(request)).get("apply", [])
    changed = []
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        for pick in picks:
            kind, _, value = pick.partition(":")
            if kind == "alias" and value:
                known = {a.lower() for a in [mag.title, *(mag.aliases or [])]}
                if value.lower() not in known:
                    mag.aliases = [*(mag.aliases or []), value]
                    changed.append(f"alias {value}")
            elif kind == "issn" and (code := normalize(value)):
                if mag.issn != code:
                    mag.issn = code
                    changed.append(f"ISSN {code}")
            elif kind == "frequency" and value in Frequency._value2member_map_:
                if mag.frequency.value != value:
                    mag.frequency = Frequency(value)
                    changed.append(f"frequency {FREQUENCY_LABELS[value]}")
            elif kind == "language" and value:
                lang = value.lower()
                if lang not in (mag.languages or []):
                    mag.languages = [*(mag.languages or []), lang]
                    changed.append(f"language {lang}")
        session.commit()
    if not changed:
        return _toast("Nothing to change.", "warn")
    response = _toast("Applied: " + ", ".join(changed) + ".")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/magazines/{mag_id:int}/metadata/back-issues")
async def add_back_issues(request: Request, mag_id: int) -> Response:
    form = await form_lists(request)
    keys = set(form.get("keys", []))
    anchor = (form.get("anchor") or [""])[-1]
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        back = _back_issues(session, request, mag) or {"issues": []}
        by_key = {b.key: b for b in back["issues"]}
        added = 0
        for key in keys & set(by_key):
            b = by_key[key]
            values = {k: b.fields.get(k) for k in ISSUE_FIELDS}
            values["special"] = bool(values.get("special"))
            cover = b.fields.get("cover_date")
            session.add(
                Issue(
                    magazine_id=mag.id,
                    key=key,
                    expected=True,
                    monitored=True,
                    status=IssueStatus.WANTED,
                    cover_date=date.fromisoformat(cover) if cover else None,
                    **values,
                )
            )
            added += 1
        note = ""
        if anchor in by_key and by_key[anchor].cover_date:
            b = by_key[anchor]
            number = b.volume_number if mag.numbering is Numbering.VOLUME else b.issue_number
            if number is not None:
                mag.anchor_date = date.fromisoformat(b.cover_date)
                mag.anchor_number = number
                note = f" Known issue set to {number} ({b.cover_date})."
        session.commit()
    response = _toast(f"Added {added} expected issues.{note}")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/lookups/dismiss")
def dismiss_notice(request: Request) -> Response:
    with request.app.state.sessions() as session:
        set_setting(session, NOTICE_KEY, True)
        session.commit()
    return Response(status_code=200, content="")


@router.post("/settings/metadata/test/{source}")
def test_source(request: Request, source: str) -> Response:
    if source not in SOURCES:
        raise HTTPException(status_code=404)
    with request.app.state.sessions() as session:
        if not enabled(session, source):
            return _toast(f"{SOURCES[source]} is off. Turn it on and save first.", "warn")
        mag = session.scalars(select(Magazine).where(Magazine.issn.is_not(None))).first()
        title = mag.title if mag else "2600: The Hacker Quarterly"
        issn = mag.issn if mag else "0749-3851"
        answer = lookups.lookup(
            session, source, issn=issn, title=title, transport=_transport(request), force=True
        )
        session.commit()
    if answer.error:
        return _toast(f"{SOURCES[source]}: {answer.error}", "bad")
    if not answer.data:
        return _toast(f"{SOURCES[source]} answered, but has nothing for {title}.", "warn")
    if source == "archive":
        return _toast(f"Internet Archive answered: {len(answer.data.get('items', []))} items.")
    return _toast(f"{SOURCES[source]} answered: {answer.record.title}.")


# Monitor pop-up (the magazine page's Monitor button)


def _monitor_popup(request, session, mag, done=None, error=None):
    states = _states(session)
    back = _back_issues(session, request, mag) if states["archive"] and done else None
    return _render(
        request,
        "magazines/_monitor.html",
        mag=mag,
        done=done,
        error=error,
        back=back,
        numbered=mag.numbering in (Numbering.ISSUE, Numbering.VOLUME),
        this_year=date.today().year,
    )


@router.get("/magazines/{mag_id:int}/monitoring", response_class=HTMLResponse)
def monitor_popup(request: Request, mag_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        return _monitor_popup(request, session, _magazine(session, mag_id))


@router.post("/magazines/{mag_id:int}/monitoring", response_class=HTMLResponse)
async def monitor_save(request: Request, mag_id: int) -> Response:
    """Change what is monitored, add the expected issues that brings in, and offer the
    Internet Archive's past issues when that lookup is on."""
    from periodicool.jobs import today_in
    from periodicool.schedule import generation_range, sync_expected_issues

    form = await form_data(request)
    today = today_in(request.app.state.settings.tz)
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        try:
            mode = MonitorMode(form.get("monitor_mode", ""))
        except ValueError:
            return _monitor_popup(request, session, mag, error="Pick which issues to monitor.")
        year = (form.get("monitor_from_year") or "").strip()
        if mode is MonitorMode.FROM_YEAR:
            if not (year.isdigit() and 1800 <= int(year) <= today.year):
                return _monitor_popup(
                    request, session, mag, error=f"Enter a year from 1800 to {today.year}."
                )
            mag.monitor_from_year = int(year)
        mag.monitor_mode = mode
        mag.monitored = form.get("monitored") == "on"
        session.flush()
        added = sync_expected_issues(session, mag, today)
        relisted = 0
        span = generation_range(mag, today)
        if form.get("monitor_existing") == "on" and span is not None and mag.monitored:
            for issue in mag.issues:
                if (
                    not issue.monitored
                    and not issue.file_path
                    and issue.cover_date is not None
                    and span[0] <= issue.cover_date <= span[1]
                ):
                    issue.monitored = True
                    relisted += 1
        session.commit()
        parts = [f"{len(added)} expected issues added"]
        if relisted:
            parts.append(f"{relisted} listed issues now monitored")
        done = ", ".join(parts) + "."
        if not _states(session)["archive"] or mode is MonitorMode.NEW_ONLY:
            response = _toast(done)
            response.headers["HX-Refresh"] = "true"
            return response
        session.refresh(mag)
        return _monitor_popup(request, session, mag, done=done)
