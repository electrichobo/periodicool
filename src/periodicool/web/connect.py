"""Settings, Connect: webhook connections (Sonarr's Webhook), their pop-up and Test.

The password is write-only: the form never carries the stored value, and an empty
field keeps it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool import notify
from periodicool.models import Notification
from periodicool.preferences import parse_url
from periodicool.web.magazines import _render, _toast
from periodicool.web.security import form_data, form_lists
from periodicool.web.sources import _learn, _name_taken

router = APIRouter(include_in_schema=False)

KIND_INFO = (
    "Posts each event as JSON to an address you choose, as Sonarr's Webhook does. "
    "Point it at Apprise, ntfy, Home Assistant, a chat bridge or your own script."
)
PASSWORD_HELP = (
    "Stored in PeriodiCool's database and never shown again. Leave empty to keep the saved one."
)
BLANK_HEADER_ROWS = 2


def connect_context(session: Session) -> dict:
    rows = session.scalars(select(Notification).order_by(Notification.name)).all()
    return {"connections": rows}


def _values(row: Notification | None) -> dict:
    if row is None:
        return {
            "name": "Webhook",
            "enabled": True,
            "url": "",
            "method": "POST",
            "username": "",
            "events": list(notify.EVENTS),
            "include_health_warnings": False,
            "headers": [],
        }
    settings = row.settings or {}
    return {
        "id": row.id,
        "name": row.name,
        "enabled": row.enabled,
        "url": row.url,
        "method": settings.get("method", "POST"),
        "username": row.username or "",
        "events": list(row.events or []),
        "include_health_warnings": bool(settings.get("include_health_warnings")),
        "headers": [list(h) for h in settings.get("headers") or []],
    }


def _form(request, values: dict, errors=None, row=None):
    headers = list(values.get("headers") or [])
    rows = headers + [["", ""]] * max(0, min(BLANK_HEADER_ROWS, notify.MAX_HEADERS - len(headers)))
    return _render(
        request,
        "settings/_connection_form.html",
        values=values,
        errors=errors or {},
        events=notify.EVENTS,
        event_help=notify.EVENT_HELP,
        header_rows=rows,
        has_password=bool(row is not None and row.password),
        password_help=PASSWORD_HELP,
        kind_info=KIND_INFO,
    )


def _parse(session: Session, form: dict, lists: dict, row: Notification | None):
    errors: dict[str, str] = {}
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Give it a name."
    elif len(name) > 100:
        errors["name"] = "Keep the name under 100 characters."
    elif _name_taken(session, Notification, name, row.id if row else None):
        errors["name"] = "Another connection already has this name."
    url, problem = parse_url(form.get("url", ""))
    if problem or not url:
        errors["url"] = problem or "Enter the address the events go to."
    method = form.get("method", "POST")
    if method not in ("POST", "PUT"):
        errors["method"] = "Pick POST or PUT."
    events = [e for e in lists.get("events", []) if e in notify.EVENTS]
    headers = []
    names, values = lists.get("header_name", []), lists.get("header_value", [])
    for header, value in zip(names, values, strict=False):
        header, value = header.strip(), value.strip()
        if not header and not value:
            continue
        if not notify.HEADER_NAME.fullmatch(header):
            errors["headers"] = f"{header or 'An empty name'} is not a valid header name."
        elif header.lower() in notify.RESERVED_HEADERS:
            errors["headers"] = f"PeriodiCool sets {header} itself."
        elif "\n" in value or "\r" in value:
            errors["headers"] = f"The value of {header} must be one line."
        headers.append([header, value])
    if len(headers) > notify.MAX_HEADERS:
        errors["headers"] = f"At most {notify.MAX_HEADERS} headers."
    data = {
        "name": name,
        "kind": "webhook",
        "enabled": form.get("enabled") == "on",
        "url": url or "",
        "username": (form.get("username") or "").strip() or None,
        "events": events,
        "settings": {
            "method": method,
            "headers": headers,
            "include_health_warnings": form.get("include_health_warnings") == "on",
        },
    }
    return errors, data, (form.get("password") or "").strip()


def _form_values(form: dict, lists: dict, data: dict) -> dict:
    return {
        **data,
        "id": form.get("id"),
        "method": data["settings"]["method"],
        "include_health_warnings": data["settings"]["include_health_warnings"],
        "headers": data["settings"]["headers"],
        "username": data["username"] or "",
    }


@router.get("/settings/connect/new", response_class=HTMLResponse)
def connection_new(request: Request, kind: str = "") -> HTMLResponse:
    if kind != "webhook":
        return _render(
            request,
            "settings/_kind_picker.html",
            what="connection",
            base="/settings/connect/new",
            kinds=[("webhook", "Webhook", KIND_INFO)],
        )
    return _form(request, _values(None))


@router.get("/settings/connect/{row_id:int}/edit", response_class=HTMLResponse)
def connection_edit(request: Request, row_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        row = session.get(Notification, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        return _form(request, _values(row), row=row)


@router.post("/settings/connect/save", response_class=HTMLResponse)
async def connection_save(request: Request) -> Response:
    form = await form_data(request)
    lists = await form_lists(request)
    with request.app.state.sessions() as session:
        row = None
        if form.get("id"):
            row = session.get(Notification, int(form["id"])) if form["id"].isdigit() else None
            if row is None:
                raise HTTPException(status_code=404)
        errors, data, password = _parse(session, form, lists, row)
        if errors:
            return _form(request, _form_values(form, lists, data), errors, row)
        if row is None:
            row = Notification(**data, password=password or None)
            session.add(row)
        else:
            for field, value in data.items():
                setattr(row, field, value)
            if password:
                row.password = password
        session.commit()
        _learn(request, password, *(v for _, v in data["settings"]["headers"]))
    return Response(status_code=204, headers={"HX-Redirect": "/settings/connect?saved=1"})


@router.post("/settings/connect/{row_id:int}/delete")
def connection_delete(request: Request, row_id: int) -> Response:
    with request.app.state.sessions() as session:
        row = session.get(Notification, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        session.delete(row)
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/settings/connect?saved=deleted"})


def _test_payload(session: Session) -> dict:
    return notify.base(session, "Test") | {
        "message": "Test from PeriodiCool. If you can read this, the webhook works."
    }


def _transport(request: Request):
    return getattr(request.app.state, "webhook_transport", None)


@router.post("/settings/connect/test")
async def connection_test_form(request: Request) -> Response:
    """Send a Test event with the values in the pop-up. Nothing is stored."""
    form = await form_data(request)
    lists = await form_lists(request)
    with request.app.state.sessions() as session:
        stored = None
        if (form.get("id") or "").isdigit():
            stored = session.get(Notification, int(form["id"]))
        errors, data, password = _parse(session, {**form, "name": "probe"}, lists, stored)
        if bad := {k: v for k, v in errors.items() if k != "name"}:
            return _toast(next(iter(bad.values())), "warn")
        probe = Notification(**data, password=password or (stored.password if stored else None))
        _learn(request, probe.password, *(v for _, v in data["settings"]["headers"]))
        sent, what = notify.deliver(probe, "Test", _test_payload(session), _transport(request))
    if sent:
        return _toast(f"Test sent; the webhook {what}.")
    return _toast(f"Test failed: {what}", "bad")


@router.post("/settings/connect/{row_id:int}/test")
def connection_test_row(request: Request, row_id: int) -> Response:
    with request.app.state.sessions() as session:
        row = session.get(Notification, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        sent, what = notify.deliver(row, "Test", _test_payload(session), _transport(request))
        notify.record_result(row, sent, what)
        session.commit()
    if sent:
        return _toast(f"Test sent to {row.name}; the webhook {what}.")
    return _toast(f"Test to {row.name} failed: {what}", "bad")
