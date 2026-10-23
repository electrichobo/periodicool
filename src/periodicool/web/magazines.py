"""Magazine screens: index (cards, table, overview), add/edit, detail, pop-ups, mass editor."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from periodicool import downloads, library, magazines, overview, search
from periodicool.clients.base import ClientError
from periodicool.jobs import today_in
from periodicool.lookups import NOTICE_KEY as LOOKUP_NOTICE_KEY
from periodicool.lookups import any_enabled as lookups_any_enabled
from periodicool.models import (
    FormatProfile,
    Frequency,
    History,
    Issue,
    Magazine,
    MonitorMode,
    Numbering,
    Release,
    ScanKind,
)
from periodicool.prowlarr import ProwlarrError, RateLimited
from periodicool.sources import Sources, transports_from
from periodicool.web.security import form_data, form_lists

router = APIRouter(include_in_schema=False)

SAVED_MESSAGES = {
    "editor": "Changes saved.",
    "deleted": "Removed from PeriodiCool. Files on disk were kept.",
}
NO_PROWLARR = "No indexer is set up yet. Add one in Settings, Indexers."


def _render(request: Request, name: str, /, **context) -> HTMLResponse:
    from periodicool.web import render

    return render(request, name, **context)


def _today(request: Request):
    return today_in(request.app.state.settings.tz)


def _toast(text: str, kind: str = "ok", status_code: int = 200, **extra) -> Response:
    trigger = {"toast": {"kind": kind, "text": text}, **extra}
    return Response(status_code=status_code, headers={"HX-Trigger": json.dumps(trigger)})


def sources_for(request: Request, session) -> Sources:
    return Sources(session, transports_from(request.app.state))


def prowlarr_client(request: Request, session) -> Sources | None:
    """Every enabled indexer entry, or None when there is none."""
    sources = sources_for(request, session)
    return sources if sources.indexers else None


def sab_client(request: Request, session) -> Sources | None:
    """Every enabled download client entry, or None when there is none."""
    sources = sources_for(request, session)
    try:
        return sources if sources.clients else None
    except ClientError:
        return None


def _choices(session) -> dict:
    return {
        "frequencies": [(f.value, overview.FREQUENCY_LABELS[f.value]) for f in Frequency],
        "numberings": [(n.value, overview.NUMBERING_LABELS[n.value]) for n in Numbering],
        "modes": [
            (MonitorMode.NEW_ONLY.value, "New issues only"),
            (MonitorMode.FROM_YEAR.value, "From a year on"),
            (MonitorMode.ALL.value, "All issues"),
        ],
        "profiles": list(session.scalars(select(FormatProfile).order_by(FormatProfile.name))),
    } | {
        "profile_options": [
            (p.id, p.name)
            for p in session.scalars(select(FormatProfile).order_by(FormatProfile.name))
        ]
    }


def _magazine(session, mag_id: int) -> Magazine:
    mag = session.get(Magazine, mag_id, options=[selectinload(Magazine.issues)])
    if mag is None:
        raise HTTPException(status_code=404, detail="No such magazine")
    return mag


# Index


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        from periodicool.covers import with_pdf_covers

        summaries = overview.all_summaries(session, _today(request))
        context = {
            "summaries": summaries,
            "pdf_covers": with_pdf_covers(request.app.state.settings.config_dir),
            "totals": overview.totals(summaries),
            "tags": overview.all_tags(session),
            "saved": SAVED_MESSAGES.get(request.query_params.get("saved", "")),
            "lookup_notice": not lookups_any_enabled(session)
            and not library.get_setting(session, LOOKUP_NOTICE_KEY, False),
        }
        return _render(request, "magazines/index.html", active="/", **context)


@router.get("/covers/{mag_id:int}")
def cover(request: Request, mag_id: int) -> Response:
    with request.app.state.sessions() as session:
        mag = session.get(Magazine, mag_id)
        path = overview.cover_file(request.app.state.settings.library_dir, mag) if mag else None
    if path is None and mag is not None:
        from periodicool.covers import cached_cover

        path = cached_cover(request.app.state.settings.config_dir, mag_id)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/magazines/refresh-all")
def refresh_all(request: Request) -> Response:
    from periodicool.schedule import sync_expected_issues

    with request.app.state.sessions() as session:
        today, created = _today(request), 0
        for mag in session.scalars(select(Magazine)):
            created += len(sync_expected_issues(session, mag, today))
        root = request.app.state.settings.library_dir
        found = library.refresh_file_info(session, root) if root.is_dir() else {"covers": 0}
        if root.is_dir():
            from periodicool.covers import refresh_covers

            found["covers"] += refresh_covers(session, root, request.app.state.settings.config_dir)
        session.commit()
    return _toast(f"Expected issues updated ({created} new). Covers found: {found['covers']}.")


@router.post("/tasks/rss")
def rss_now(request: Request) -> Response:
    with request.app.state.sessions() as session:
        client = prowlarr_client(request, session)
        if client is None:
            return _toast("No indexer is set up yet (Settings, Indexers).", "warn")
        try:
            result = search.rss_sync(session, client, _today(request))
        except ProwlarrError as exc:
            return _toast(f"RSS sync failed: {exc}", "bad")
        session.commit()
    return _toast(f"RSS sync: {result.seen} seen, {len(result.accepted)} accepted.")


@router.post("/magazines/search-missing")
def search_missing(request: Request) -> Response:
    with request.app.state.sessions() as session:
        client = prowlarr_client(request, session)
        if client is None:
            return _toast("No indexer is set up yet (Settings, Indexers).", "warn")
        results = search.wanted_search(session, client, _today(request))
        session.commit()
    found = sum(r.new_accepted for r in results)
    return _toast(
        f"Searched {len(results)} magazines (oldest first, five at most); {found} new matches."
    )


# Add and edit


def _lookup_new_issn(request, session, mag, old_issn) -> None:
    """One lookup when a magazine gets a new ISSN, for the sources that are on.

    The answer goes to the cache only; it helps recognize files and fills the
    Metadata pop-up. Failures are cached quietly.
    """
    if not mag.issn or mag.issn == old_issn or not lookups_any_enabled(session):
        return
    from periodicool.lookups.service import records

    records(session, mag, transport=getattr(request.app.state, "lookup_transport", None))


def _form_page(request, session, *, mag=None, parsed=None, status_code=200, modal=False):
    values = parsed.raw if parsed else magazines.form_values(mag)
    if parsed:
        for name in magazines.BOOL_FIELDS:
            values[name] = parsed.raw.get(name) in ("on", "true", "1")
    name = "magazines/_form_modal.html" if modal else "magazines/form.html"
    response = _render(
        request,
        name,
        active="/",
        mag=mag,
        values=values,
        errors=parsed.errors if parsed else {},
        **_choices(session),
    )
    response.status_code = status_code
    return response


@router.get("/magazines/add", response_class=HTMLResponse)
def add_page(request: Request, term: str = "") -> HTMLResponse:
    with request.app.state.sessions() as session:
        return _render(
            request,
            "magazines/form.html",
            active="/",
            term=term,
            lookups_on=lookups_any_enabled(session),
            indexers_on=bool(sources_for(request, session).indexers),
        )


def _add_cards(request, session, term: str):
    """Cards for the add search: release names from the indexers (cached) and, when
    the user turned them on, catalog matches. Returns (cards, problems)."""
    from periodicool import add_search
    from periodicool.issn import normalize
    from periodicool.lookups import service as lookups

    issn = normalize(term)
    transport = getattr(request.app.state, "lookup_transport", None)
    records, problems = [], []
    for src in ("loc", "wikidata"):
        found, error = lookups.candidates(
            session, src, issn=issn, title=None if issn else term, transport=transport
        )
        records += found
        if error:
            problems.append(error)
    names: list[str] = []
    indexers = sources_for(request, session)
    if indexers.indexers and not issn:
        names, error = search.search_term(session, indexers, term, _today(request))
        if error:
            problems.append(error)
    tracked = {}
    for mag in session.scalars(select(Magazine)):
        for name in [mag.title, *(mag.aliases or [])]:
            tracked.setdefault(add_search.title_key(name), mag.id)
    cards = add_search.merge(term, records, add_search.from_releases(names), tracked)
    return cards, problems


def _card_values(card) -> dict:
    values = magazines.form_values(None) | {"title": card.title}
    if card.frequency:
        values["frequency"] = card.frequency
    g = card.guess
    if g.numbering:
        values["numbering"] = g.numbering.value
    if g.anchor_date:
        values["anchor_date"] = g.anchor_date.isoformat()
    if g.anchor_number:
        values["anchor_number"] = str(g.anchor_number)
    if card.issns:
        values["issn"] = card.issns[0]
    if card.alt_titles:
        values["aliases"] = ", ".join(card.alt_titles[:5])
    if g.region:
        values["region"] = g.region
    language = g.language or card.first("language")
    if language:
        values["languages"] = language.lower()
    return values


@router.post("/magazines/add/search", response_class=HTMLResponse)
async def add_search_results(request: Request) -> HTMLResponse:
    form = await form_data(request)
    term = (form.get("term") or "").strip()[:200]
    with request.app.state.sessions() as session:
        if not term:
            return HTMLResponse('<p class="field__help">Type a title or an ISSN first.</p>')
        cards, problems = _add_cards(request, session, term)
        session.commit()
        return _render(
            request,
            "magazines/_add_results.html",
            term=term,
            cards=cards[:12],
            problems=problems,
            lookups_on=lookups_any_enabled(session),
            indexers_on=bool(sources_for(request, session).indexers),
            frequency_labels=overview.FREQUENCY_LABELS,
        )


def _add_form(
    request, session, values: dict, errors=None, *, term="", key="", card=None, page=False
):
    return _render(
        request,
        "magazines/add_form_page.html" if page else "magazines/_add_form.html",
        active="/",
        values=values,
        errors=errors or {},
        term=term,
        key=key,
        card=card,
        **_choices(session),
    )


@router.post("/magazines/add/pick", response_class=HTMLResponse)
async def add_pick(request: Request) -> HTMLResponse:
    """The short Add form, filled in from the picked card."""
    form = await form_data(request)
    term, key = (form.get("term") or "").strip(), form.get("key") or ""
    with request.app.state.sessions() as session:
        cards, _ = _add_cards(request, session, term)  # all cached by now
        session.commit()
        card = next((c for c in cards if c.key == key), None)
        if card is None:
            return _add_form(request, session, magazines.form_values(None) | {"title": term})
        return _add_form(request, session, _card_values(card), term=term, key=key, card=card)


@router.get("/magazines/add/manual", response_class=HTMLResponse)
def add_manual(request: Request, title: str = "", term: str = "") -> HTMLResponse:
    with request.app.state.sessions() as session:
        values = magazines.form_values(None) | {"title": (title or term).strip()[:200]}
        return _add_form(request, session, values)


@router.post("/magazines/add", response_class=HTMLResponse)
async def add_submit(request: Request) -> Response:
    form = await form_data(request)
    popup = bool(request.headers.get("hx-request"))
    with request.app.state.sessions() as session:
        parsed = magazines.parse_form(session, form)
        if not parsed.ok:
            values = dict(parsed.raw)
            for name in magazines.BOOL_FIELDS:
                values[name] = parsed.raw.get(name) in ("on", "true", "1")
            response = _add_form(
                request,
                session,
                values,
                parsed.errors,
                term=form.get("pick_term", ""),
                key=form.get("pick_key", ""),
                page=not popup,
            )
            if not popup:
                response.status_code = 422
            return response
        mag = magazines.apply_values(Magazine(issues=[]), parsed.values)
        session.add(mag)
        session.flush()
        if form.get("pick_term") and form.get("pick_key"):
            # What the catalog said about the picked card becomes this magazine's
            # lookup answers, so its Metadata pop-up does not ask again.
            from periodicool.lookups import service as lookups

            cards, _ = _add_cards(request, session, form["pick_term"])
            card = next((c for c in cards if c.key == form["pick_key"]), None)
            if card is not None and card.records:
                lookups.remember(session, mag, card.records)
        else:
            _lookup_new_issn(request, session, mag, None)
        from periodicool.schedule import sync_expected_issues

        sync_expected_issues(session, mag, _today(request))
        session.commit()
        if form.get("search_now") == "on":
            client = prowlarr_client(request, session)
            if client is not None:
                try:
                    magazines.automatic_search(
                        session, mag, _today(request), client, sab_client(request, session)
                    )
                    session.commit()
                except ProwlarrError, RateLimited:
                    session.rollback()
        if popup:
            return Response(status_code=204, headers={"HX-Redirect": f"/magazines/{mag.id}"})
        return RedirectResponse(f"/magazines/{mag.id}", status_code=303)


@router.post("/magazines/preview", response_class=HTMLResponse)
async def add_preview(request: Request) -> HTMLResponse:
    form = await form_data(request)
    with request.app.state.sessions() as session:
        parsed = magazines.parse_form(session, form)
        limit = 3 if form.get("short") else 8
        result = magazines.preview(session, parsed.values, _today(request), limit=limit)
        return _render(request, "magazines/_preview.html", preview=result)


@router.get("/magazines/{mag_id:int}/edit", response_class=HTMLResponse)
def edit_modal(request: Request, mag_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        return _form_page(request, session, mag=_magazine(session, mag_id), modal=True)


@router.post("/magazines/{mag_id:int}/edit", response_class=HTMLResponse)
async def edit_submit(request: Request, mag_id: int) -> Response:
    form = await form_data(request)
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        parsed = magazines.parse_form(session, form)
        if not parsed.ok:
            return _form_page(request, session, mag=mag, parsed=parsed, modal=True)
        old_issn = mag.issn
        magazines.apply_values(mag, parsed.values)
        _lookup_new_issn(request, session, mag, old_issn)
        from periodicool.schedule import sync_expected_issues

        sync_expected_issues(session, mag, _today(request))
        session.commit()
    response = _toast("Saved.")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/magazines/{mag_id:int}/delete")
def delete(request: Request, mag_id: int) -> Response:
    with request.app.state.sessions() as session:
        magazines.delete_magazines(session, [mag_id])
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/?saved=deleted"})


# Detail


@router.get("/magazines/{mag_id:int}", response_class=HTMLResponse)
def detail(request: Request, mag_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        summary, groups = overview.year_groups(mag, _today(request))
        return _render(
            request,
            "magazines/detail.html",
            active="/",
            mag=mag,
            s=summary,
            groups=groups,
            chart=overview.chart_rows(groups),
            folder=overview.magazine_folder(session, mag),
            has_cover=overview.cover_file(request.app.state.settings.library_dir, mag) is not None,
        )


@router.post("/magazines/{mag_id:int}/monitor")
def toggle_magazine(request: Request, mag_id: int) -> Response:
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        mag.monitored = not mag.monitored
        session.commit()
        state = "monitored" if mag.monitored else "not monitored"
    return _toast(f"{mag.title} is {state}.")


@router.post("/magazines/{mag_id:int}/years/{year:int}/monitor")
async def toggle_year(request: Request, mag_id: int, year: int) -> Response:
    form = await form_data(request)
    want = form.get("monitored") == "on"
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        count = magazines.set_year_monitored(session, mag, year, want)
        session.commit()
    verb = "Monitoring" if want else "Stopped monitoring"
    response = _toast(f"{verb} {count} issues in {year}.")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/issues/{issue_id:int}/monitor")
def toggle_issue(request: Request, issue_id: int) -> Response:
    with request.app.state.sessions() as session:
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(status_code=404)
        issue.monitored = not issue.monitored
        session.commit()
        state = "monitored" if issue.monitored else "not monitored"
    return _toast(f"{issue.key} is {state}.")


@router.post("/issues/{issue_id:int}/unlink")
def unlink(request: Request, issue_id: int) -> Response:
    with request.app.state.sessions() as session:
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(status_code=404)
        old = magazines.unlink_issue(issue)
        session.commit()
    response = _toast(f"Unlinked {old}. The file is still in the library.")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/magazines/{mag_id:int}/search")
async def auto_search(request: Request, mag_id: int) -> Response:
    form = await form_data(request)
    key = form.get("issue") or None
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        client = prowlarr_client(request, session)
        if client is None:
            return _toast("No indexer is set up yet (Settings, Indexers).", "warn")
        try:
            result = magazines.automatic_search(
                session, mag, _today(request), client, sab_client(request, session), key
            )
        except (ProwlarrError, RateLimited) as exc:
            session.rollback()
            return _toast(f"Search stopped: {exc}", "bad")
        session.commit()
    parts = [f"{result.found} accepted releases"]
    if result.grabbed:
        parts.append("grabbed " + ", ".join(result.grabbed))
    elif key:
        parts.append(f"nothing grabbed for {key}")
    parts += result.problems[:2]
    kind = "ok" if result.grabbed or not result.problems else "warn"
    return _toast("Search done: " + "; ".join(parts) + ".", kind)


# Pop-ups


def _age(published: datetime | None, now: datetime) -> str:
    if published is None:
        return ""
    if published.tzinfo is None:
        from datetime import UTC

        published = published.replace(tzinfo=UTC)
    days = (now - published).days
    return f"{days} d" if days >= 1 else "today"


@router.get("/magazines/{mag_id:int}/interactive", response_class=HTMLResponse)
def interactive(request: Request, mag_id: int, issue: str = "", force: bool = False):
    from datetime import UTC

    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        client = prowlarr_client(request, session)
        error, rows, from_cache, skipped = None, [], False, {}
        if client is None:
            error = NO_PROWLARR
        else:
            try:
                result = search.search_magazine(session, client, mag, _today(request), force=force)
                session.commit()
                from_cache, skipped = result.from_cache, result.skipped
                now = datetime.now(UTC)
                for rel, ev in result.candidates:
                    if issue and ev.issue_key != issue:
                        continue
                    rows.append(
                        {
                            "rel": rel,
                            "ev": ev,
                            "age": _age(rel.publish_date, now),
                            "formats": list(ev.parsed.formats) if ev.parsed else [],
                            "true_pdf": bool(ev.parsed and ev.parsed.true_pdf),
                            "other_magazine": ev.magazine is not None and ev.magazine.id != mag.id,
                        }
                    )
                rows.sort(key=lambda r: (not r["ev"].accepted, r["rel"].rank or [9]))
            except (ProwlarrError, RateLimited) as exc:
                session.rollback()
                error = f"Search stopped: {exc}"
        return _render(
            request,
            "magazines/_interactive.html",
            mag=mag,
            rows=rows,
            issue=issue,
            error=error,
            from_cache=from_cache,
            skipped=skipped,
        )


@router.post("/releases/{release_id:int}/grab")
def grab_release(request: Request, release_id: int) -> Response:
    with request.app.state.sessions() as session:
        rel = session.get(Release, release_id)
        if rel is None:
            raise HTTPException(status_code=404)
        prowlarr, client = prowlarr_client(request, session), sab_client(request, session)
        if prowlarr is None or client is None:
            return _toast("An indexer and a download client both need to be set up first.", "warn")
        result = downloads.grab(session, rel, prowlarr, client, _today(request))
        session.commit()
    if result.ok:
        return _toast(f"Sent to {result.client_name}: {rel.title}")
    return _toast("Not grabbed: " + "; ".join(result.reasons[:3]), "bad")


@router.get("/issues/{issue_id:int}", response_class=HTMLResponse)
def issue_detail(request: Request, issue_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(status_code=404)
        mag = issue.magazine
        row = overview.IssueRow(
            issue,
            overview.issue_state(issue, mag, _today(request)),
            overview.issue_label(issue, mag),
        )
        events = list(
            session.scalars(
                select(History)
                .where(History.magazine_id == mag.id, History.issue_key == issue.key)
                .order_by(History.date.desc())
                .limit(50)
            )
        )
        return _render(request, "magazines/_issue.html", mag=mag, row=row, events=events)


@router.get("/magazines/{mag_id:int}/scan", response_class=HTMLResponse)
def scan_preview(request: Request, mag_id: int) -> HTMLResponse:
    """Plan a scan of just this magazine's folder: a preview of moves and renames."""
    settings = request.app.state.settings
    with request.app.state.sessions() as session:
        mag = _magazine(session, mag_id)
        folder = overview.magazine_folder(session, mag)
        root = settings.library_dir / folder if folder else None
        scan, items = None, []
        if root is not None and root.is_dir():
            scan = library.plan_scan(session, root, ScanKind.LIBRARY, settings.library_dir)
            session.commit()
            from periodicool.models import ImportItem

            items = list(
                session.scalars(
                    select(ImportItem).where(ImportItem.scan_id == scan.id).order_by(ImportItem.id)
                )
            )
        return _render(
            request,
            "magazines/_scan.html",
            mag=mag,
            folder=folder,
            scan=scan,
            items=items,
            library=settings.library_dir,
        )


@router.post("/scans/{scan_id:int}/apply")
def apply_scan(request: Request, scan_id: int) -> Response:
    settings = request.app.state.settings
    with request.app.state.sessions() as session:
        result = library.apply_scan(session, scan_id, settings.library_dir)
        session.commit()
    text = ", ".join(f"{k} {v}" for k, v in result.items())
    response = _toast(f"Applied: {text}.", "ok" if not result.get("failed") else "warn")
    response.headers["HX-Refresh"] = "true"
    return response


# Mass editor


@router.get("/magazines/editor", response_class=HTMLResponse)
def editor(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        summaries = overview.all_summaries(session, _today(request))
        return _render(
            request,
            "magazines/editor.html",
            active="/",
            summaries=summaries,
            saved=SAVED_MESSAGES.get(request.query_params.get("saved", "")),
            **_choices(session),
        )


@router.post("/magazines/editor")
async def editor_submit(request: Request) -> Response:
    form = await form_lists(request)
    first = {k: v[-1] for k, v in form.items()}
    ids = [int(i) for i in form.get("ids", []) if i.isdigit()]
    if not ids:
        return _toast("Select at least one magazine.", "warn")
    with request.app.state.sessions() as session:
        if first.get("action") == "delete":
            magazines.delete_magazines(session, ids)
            session.commit()
            return Response(
                status_code=204, headers={"HX-Redirect": "/magazines/editor?saved=deleted"}
            )
        edit = magazines.MassEdit(
            monitored={"yes": True, "no": False}.get(first.get("monitored", "")),
            monitor_mode=MonitorMode(first["monitor_mode"]) if first.get("monitor_mode") else None,
            format_profile_id=int(first["format_profile_id"])
            if first.get("format_profile_id", "").isdigit()
            else None,
            add_tags=magazines.split_list(first.get("add_tags", "")),
            remove_tags=magazines.split_list(first.get("remove_tags", "")),
        )
        magazines.mass_edit(session, ids, edit)
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/magazines/editor?saved=editor"})
