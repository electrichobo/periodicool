"""Settings, Indexers and Download clients: the lists, their pop-up forms and tests.

Keys and passwords are write-only: the form never carries the stored value, and an
empty key field keeps it.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from periodicool import indexer_kinds, sources
from periodicool.clients import registry
from periodicool.clients.base import ClientError
from periodicool.models import ACTIVE_GRAB, DownloadClient, Grab, Indexer, Release
from periodicool.preferences import parse_url
from periodicool.prowlarr import ProwlarrError
from periodicool.web.magazines import _render, _toast
from periodicool.web.security import form_data, form_lists

router = APIRouter(include_in_schema=False)

KEY_HELP = (
    "Stored in PeriodiCool's database and never shown again. Leave empty to keep the saved key."
)


def _learn(request: Request, *secrets: str | None) -> None:
    logs = getattr(request.app.state, "logs", None)
    if logs is not None:
        logs.redactor.learn([s for s in secrets if s])


def _int(value: str, low: int, high: int, label: str, errors: dict, name: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        errors[name] = f"{label} must be a whole number."
        return None
    if not low <= number <= high:
        errors[name] = f"{label} must be between {low} and {high}."
        return None
    return number


def _name_taken(session: Session, model, name: str, row_id: int | None) -> bool:
    query = select(model.id).where(func.lower(model.name) == name.lower())
    if row_id is not None:
        query = query.where(model.id != row_id)
    return session.scalar(query) is not None


def _common(session: Session, model, form: dict, row_id: int | None, kinds: dict) -> tuple:
    errors: dict[str, str] = {}
    kind = form.get("kind", "")
    if kind not in kinds:
        errors["kind"] = "Pick a kind."
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Give it a name."
    elif len(name) > 100:
        errors["name"] = "Keep the name under 100 characters."
    elif _name_taken(session, model, name, row_id):
        errors["name"] = "Another entry already has this name."
    url, problem = parse_url(form.get("url", ""))
    if problem or not url:
        errors["url"] = problem or "Enter the address, for example http://prowlarr:9696."
    priority = _int(form.get("priority", ""), 1, 50, "Priority", errors, "priority")
    return errors, kind, name, url, priority


# Indexers


def _form_bools(form: dict) -> list[str]:
    """Switch fields on the indexer form, so a redisplay keeps them as set."""
    kind = indexer_kinds.KINDS.get(form.get("kind", ""))
    names = [name for name, _, _ in TOGGLES]
    return names + [f.name for f in (kind.fields if kind else ()) if f.type == "bool"]


def _indexer_rows(session: Session):
    return list(session.scalars(select(Indexer).order_by(Indexer.priority, Indexer.id)))


def indexer_context(session: Session) -> dict:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    rows = []
    for row in _indexer_rows(session):
        until = row.backoff_until
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        rows.append(
            {
                "row": row,
                "kind_label": sources.INDEXER_KINDS.get(row.kind, row.kind),
                "has_key": bool(sources.indexer_key(session, row)),
                "backoff": until if until and until > now else None,
            }
        )
    return {"indexer_rows": rows}


TOGGLES = (
    ("enable_rss", "Enable RSS", "Look for new releases in the indexer's RSS feed."),
    (
        "enable_automatic_search",
        "Enable automatic search",
        "Used by scheduled searches, Wanted searches and the Search buttons.",
    ),
    (
        "enable_interactive_search",
        "Enable interactive search",
        "Used by interactive search and the Add magazine search.",
    ),
)
# Bounds for the numeric settings: (low, high, label).
INDEXER_NUMBERS = {
    "seed_time_min": (0, 525_600, "Seed time"),
    "min_seeders": (0, 10_000, "Minimum seeders"),
    "daily_cap": (1, 100_000, "API calls per day"),
    "grab_cap": (1, 100_000, "Downloads per day"),
}


def _indexer_values(row: Indexer | None, kind: str, preset: str = "") -> dict:
    found = indexer_kinds.KINDS.get(row.kind if row else kind)
    values = {name: True for name, _, _ in TOGGLES}
    if found is not None:
        values |= {f.name: _blank(f.default) for f in found.fields if f.store == "settings"}
    if row is None:
        values |= {
            "kind": kind,
            "name": indexer_kinds.INDEXER_KINDS.get(kind, ""),
            "priority": 25,
            "categories": str(indexer_kinds.MAGAZINES),
            "download_client_id": "",
        }
        if chosen := indexer_kinds.preset(kind, preset):
            values |= {"name": chosen.name, "url": chosen.url, "api_path": chosen.api_path}
        return values
    values |= {k: _blank(v) for k, v in (row.settings or {}).items()}
    values |= {name: getattr(row, name) for name, _, _ in TOGGLES}
    return values | {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "url": row.url,
        "priority": row.priority,
        "categories": ", ".join(map(str, row.categories or [])),
        "download_client_id": row.download_client_id or "",
    }


def _blank(value) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return value


def _indexer_form(request, session, values: dict, errors=None, row=None):
    kind = indexer_kinds.KINDS.get(values.get("kind", ""))
    protocols = (
        {"usenet", "torrent"}
        if values.get("kind") == "prowlarr"
        else {kind.protocol if kind else "usenet"}
    )
    clients = [
        (c.id, c.name)
        for c in session.scalars(
            select(DownloadClient).order_by(DownloadClient.priority, DownloadClient.id)
        )
        if c.protocol in protocols
    ]
    return _render(
        request,
        "settings/_indexer_form.html",
        values=values,
        errors=errors or {},
        kind=kind,
        toggles=TOGGLES,
        clients=clients,
        has_key=bool(row is not None and sources.indexer_key(session, row)),
        env_fallback=_uses_env(session, row, sources.indexer_key),
        key_help=KEY_HELP,
    )


def _parse_indexer(
    session: Session, form: dict, row: Indexer | None, lists: dict | None = None
) -> tuple[dict, dict]:
    errors, kind, name, url, priority = _common(
        session, Indexer, form, row.id if row else None, sources.INDEXER_KINDS
    )
    lists = lists or {}
    categories: list[int] = []
    typed = ",".join(lists.get("categories") or [form.get("categories") or ""])
    for part in typed.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            errors["categories"] = "Categories are numbers separated by commas, such as 7010."
            break
        if int(part) not in categories:
            categories.append(int(part))
    found = indexer_kinds.KINDS.get(kind)
    settings: dict = {}
    for f in found.fields if found else ():
        if f.store != "settings":
            continue
        raw = (form.get(f.name) or "").strip()
        if f.type == "bool":
            settings[f.name] = form.get(f.name) == "on"
        elif f.type == "int":
            low, high, label = INDEXER_NUMBERS.get(f.name, (0, 1_000_000, f.label))
            value = _int(raw, low, high, label, errors, f.name)
            if value is not None:
                settings[f.name] = value
        elif f.type == "decimal":
            if raw:
                try:
                    number = float(raw)
                    if not 0 <= number <= 100:
                        raise ValueError
                    settings[f.name] = number
                except ValueError:
                    errors[f.name] = (
                        f"The {f.label.lower()} is a number from 0 to 100, such as 1.5."
                    )
        elif f.name == "api_path":
            path = "/" + raw.strip("/") if raw.strip("/") else indexer_kinds.DEFAULT_API_PATH
            if not re.fullmatch(r"(/[\w.\-~]+)+", path):
                errors[f.name] = "The API path is a path such as /api."
            settings[f.name] = path
        elif f.name == "additional_parameters":
            _, problem = indexer_kinds.parse_parameters(raw)
            if problem:
                errors[f.name] = problem
            settings[f.name] = raw
        else:
            settings[f.name] = raw
    toggles = {name: form.get(name) == "on" for name, _, _ in TOGGLES}
    client_id = None
    if (form.get("download_client_id") or "").isdigit():
        client = session.get(DownloadClient, int(form["download_client_id"]))
        if client is None:
            errors["download_client_id"] = "That download client no longer exists."
        else:
            client_id = client.id
    key = (form.get("api_key") or "").strip()
    if _key_missing(session, Indexer, kind, row, key):
        errors["api_key"] = "Enter the API key."
    return errors, {
        "kind": kind,
        "protocol": indexer_kinds.INDEXER_PROTOCOLS.get(kind, "usenet"),
        "name": name,
        "url": url,
        "priority": priority or 25,
        **toggles,
        "download_client_id": client_id,
        "categories": categories,
        "settings": settings,
        "api_key": key,
    }


def _key_missing(session: Session, model, kind: str, row, key: str) -> bool:
    """No key typed, none saved, and no environment fallback. The environment variable
    only covers the first Prowlarr and the first SABnzbd entry."""
    if key:
        return False
    getter = sources.indexer_key if model is Indexer else sources.client_key
    if row is not None:
        return not getter(session, row)
    env = {"prowlarr": sources.PROWLARR_KEY_ENV, "sabnzbd": sources.SAB_KEY_ENV}.get(kind)
    first = session.scalar(select(model.id).where(model.kind == kind).limit(1)) is None
    return not (env and first and os.environ.get(env))


def _uses_env(session: Session, row, getter) -> bool:
    return row is not None and not row.api_key and bool(getter(session, row))


@router.get("/settings/indexers/new", response_class=HTMLResponse)
def indexer_new(request: Request, kind: str = "", preset: str = "") -> HTMLResponse:
    if kind not in sources.INDEXER_KINDS:
        kinds = indexer_kinds.KINDS
        usenet = [(k.kind, k.label, k.info) for k in kinds.values() if k.protocol == "usenet"] + [
            (
                f"newznab&preset={urllib.parse.quote(p.name)}",
                p.name,
                f"Newznab. Fills in {p.url}; you add your API key.",
            )
            for p in kinds["newznab"].presets
        ]
        torrents = [(k.kind, k.label, k.info) for k in kinds.values() if k.protocol == "torrent"]
        return _render(
            request,
            "settings/_kind_picker.html",
            what="indexer",
            base="/settings/indexers/new",
            intro="Pick an indexer kind, or one of the usenet indexers below to fill in its "
            "address, as in Sonarr. Nothing is contacted until you test or save.",
            groups=[("Usenet", usenet), ("Torrents", torrents)],
        )
    with request.app.state.sessions() as session:
        return _indexer_form(request, session, _indexer_values(None, kind, preset))


@router.get("/settings/indexers/{row_id:int}/edit", response_class=HTMLResponse)
def indexer_edit(request: Request, row_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        row = session.get(Indexer, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        return _indexer_form(request, session, _indexer_values(row, row.kind), row=row)


@router.post("/settings/indexers/save", response_class=HTMLResponse)
async def indexer_save(request: Request) -> Response:
    form = await form_data(request)
    lists = await form_lists(request)
    with request.app.state.sessions() as session:
        row = None
        if form.get("id"):
            row = session.get(Indexer, int(form["id"])) if form["id"].isdigit() else None
            if row is None:
                raise HTTPException(status_code=404)
        errors, data = _parse_indexer(session, form, row, lists)
        if errors:
            values = {**form, **{k: form.get(k) == "on" for k in _form_bools(form)}}
            values["categories"] = ",".join(lists.get("categories") or [form.get("categories", "")])
            return _indexer_form(request, session, values, errors, row)
        key = data.pop("api_key")
        if row is None:
            row = Indexer(**data, api_key=key or None)
            session.add(row)
        else:
            if row.kind != data["kind"]:
                raise HTTPException(status_code=400)
            for field, value in data.items():
                setattr(row, field, value)
            if key:
                row.api_key = key
        session.commit()
        _learn(request, key)
    return Response(status_code=204, headers={"HX-Redirect": "/settings/indexers?saved=1"})


@router.post("/settings/indexers/{row_id:int}/delete")
def indexer_delete(request: Request, row_id: int) -> Response:
    with request.app.state.sessions() as session:
        row = session.get(Indexer, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        kept = session.scalar(
            select(func.count()).select_from(Release).where(Release.source_id == row_id)
        )
        session.delete(row)
        session.commit()
    saved = "deleted-kept" if kept else "deleted"
    return Response(status_code=204, headers={"HX-Redirect": f"/settings/indexers?saved={saved}"})


def _test_indexer(request, session, row: Indexer) -> str:
    from periodicool.search import indexer_limits

    client = sources.build_indexer(session, row, sources.transports_from(request.app.state))
    if client is None:
        raise ProwlarrError("the address and API key are both needed")
    if row.kind in ("newznab", "torznab"):
        caps = client.caps()
        title = caps["title"] or row.name
        if not caps["search"]:
            return f"{title} answered, but says search is turned off."
        mags = "has" if caps["magazines"] else "does not list"
        return f"{title} answered; it {mags} a magazines category (7000 or 7010)."
    limits = indexer_limits(session, client)
    return f"Prowlarr answered; {len(limits)} indexers carry magazines."


@router.post("/settings/indexers/test")
async def indexer_test_form(request: Request) -> Response:
    """Test the values in the pop-up before saving. Nothing is stored."""
    form = await form_data(request)
    with request.app.state.sessions() as session:
        stored = None
        if (form.get("id") or "").isdigit():
            stored = session.get(Indexer, int(form["id"]))
        url, problem = parse_url(form.get("url", ""))
        if problem or not url:
            return _toast(problem or "Enter the address first.", "warn")
        kind = form.get("kind", "")
        if kind not in sources.INDEXER_KINDS:
            return _toast("Pick a kind first.", "warn")
        errors, data = _parse_indexer(session, {**form, "name": "probe"}, stored)
        if bad := {k: v for k, v in errors.items() if k not in ("name", "api_key")}:
            return _toast(next(iter(bad.values())), "warn")
        probe = Indexer(
            id=stored.id if stored else None,
            name=(form.get("name") or kind).strip(),
            kind=kind,
            url=url,
            api_key=(form.get("api_key") or "").strip() or (stored.api_key if stored else None),
            settings=data["settings"],
            categories=data["categories"],
        )
        _learn(request, probe.api_key)
        try:
            text = _test_indexer(request, session, probe)
        except ProwlarrError as exc:
            session.rollback()
            return _toast(f"Test failed: {exc}", "bad")
        session.commit()
    return _toast(text)


@router.post("/settings/indexers/categories", response_class=HTMLResponse)
async def indexer_categories(request: Request) -> HTMLResponse:
    """The categories the indexer reports (Newznab caps), as ticks, as Sonarr loads
    them. Prowlarr's combined endpoint has none, so it gets Newznab's book categories.
    Nothing is stored."""
    form = await form_data(request)
    lists = await form_lists(request)
    typed = ",".join(lists.get("categories") or [form.get("categories") or ""])
    picked = [int(p) for p in typed.replace(" ", "").split(",") if p.isdigit()]
    values = {"categories": typed}
    available, problem = [], None
    kind = form.get("kind", "")
    with request.app.state.sessions() as session:
        if kind == "prowlarr":
            available = list(indexer_kinds.BOOK_CATEGORIES)
        else:
            stored = None
            if (form.get("id") or "").isdigit():
                stored = session.get(Indexer, int(form["id"]))
            errors, data = _parse_indexer(session, {**form, "name": "probe"}, stored, lists)
            url = data.get("url")
            if not url or errors.get("url"):
                problem = "Enter the indexer's address first."
            elif bad := errors.get("api_path") or errors.get("additional_parameters"):
                problem = bad
            else:
                probe = Indexer(
                    id=stored.id if stored else None,
                    name=(form.get("name") or kind).strip(),
                    kind=kind,
                    url=url,
                    api_key=data["api_key"] or (stored.api_key if stored else None),
                    settings=data["settings"],
                    categories=[],
                )
                _learn(request, probe.api_key)
                client = sources.build_indexer(
                    session, probe, sources.transports_from(request.app.state)
                )
                try:
                    if client is None:
                        raise ProwlarrError("the address and API key are both needed")
                    available = client.caps()["categories"]
                    session.commit()
                except ProwlarrError as exc:
                    session.rollback()
                    problem = str(exc)
    if available:
        # Magazines first, as the categories that matter here.
        available.sort(key=lambda c: (c[0] // 1000 != 7, c[0] != 7010, c[0]))
        picked = picked or [indexer_kinds.MAGAZINES]
    return _render(
        request,
        "settings/_indexer_categories.html",
        values=values,
        errors={},
        available=available,
        picked=picked,
        load_error=problem,
    )


@router.post("/settings/indexers/{row_id:int}/test", response_class=HTMLResponse)
def indexer_test_row(request: Request, row_id: int) -> HTMLResponse:
    """A saved entry's test: Prowlarr lists its indexers with today's limits."""
    from periodicool.search import indexer_limits

    with request.app.state.sessions() as session:
        row = session.get(Indexer, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        client = sources.build_indexer(session, row, sources.transports_from(request.app.state))
        error, limits = None, []
        if client is None:
            error = "The address or API key is missing. Edit the entry."
        else:
            try:
                limits = indexer_limits(session, client)
                session.commit()
            except ProwlarrError as exc:
                error = f"{row.name} did not answer: {exc}"
        return _render(
            request, "settings/_indexers.html", limits=limits, error=error, entry=row.name
        )


# Download clients


def client_context(session: Session) -> dict:
    rows = []
    for row in session.scalars(
        select(DownloadClient).order_by(DownloadClient.priority, DownloadClient.id)
    ):
        active = session.scalar(
            select(func.count()).select_from(Grab).where(Grab.client_id == row.id)
        )
        kind = registry.KINDS.get(row.kind)
        settings = row.settings or {}
        where = row.url
        if kind is not None and kind.local:
            where = f"{settings.get('drop_folder', '')} → {settings.get('watch_folder', '')}"
        rows.append(
            {
                "row": row,
                "kind_label": sources.CLIENT_KINDS.get(row.kind, row.kind),
                # Only a kind that cannot work without its secret (SABnzbd) is flagged.
                "missing_key": bool(kind and kind.required_secret)
                and not sources.client_key(session, row),
                "where": where,
                "grabs": active,
            }
        )
    mappings = [
        {"client": item["row"], "index": i, "remote": pair[0], "local": pair[1]}
        for item in rows
        for i, pair in enumerate(item["row"].path_map or [])
    ]
    return {"client_rows": rows, "mappings": mappings}


def _client_values(row: DownloadClient | None, kind: str) -> dict:
    """Form values: the kind's defaults, overlaid with what the entry has saved."""
    values = registry.defaults(kind)
    if row is None:
        return values | {
            "kind": kind,
            "name": sources.CLIENT_KINDS.get(kind, ""),
            "priority": 1,
            "enabled": True,
        }
    return (
        values
        | dict(row.settings or {})
        | {
            "id": row.id,
            "kind": row.kind,
            "name": row.name,
            "priority": row.priority,
            "enabled": row.enabled,
            "category": row.category,
            "username": row.username or "",
        }
    )


def _client_form(request, session, values: dict, errors=None, row=None):
    kind = registry.KINDS.get(values.get("kind", ""))
    saved = {
        "api_key": bool(row is not None and sources.client_key(session, row))
        if kind is not None and kind.required_secret == "api_key"
        else bool(row is not None and row.api_key),
        "password": bool(row is not None and row.password),
    }
    return _render(
        request,
        "settings/_client_form.html",
        values=values,
        errors=errors or {},
        kind=kind,
        saved=saved,
        env_fallback=_uses_env(session, row, sources.client_key),
        kinds=sources.CLIENT_KINDS,
        key_help=KEY_HELP,
    )


def _parse_client(
    session, lists: dict, row: DownloadClient | None, downloads_dir: Path | None = None
) -> tuple[dict, dict, dict]:
    """(errors, row values, secrets typed in). Fields come from the kind's registry entry;
    a single "url" (the command line, older forms) is split into host, port, SSL and base."""
    form = {k: v[-1] for k, v in lists.items()}
    errors: dict[str, str] = {}
    kind = registry.KINDS.get(form.get("kind", ""))
    if kind is None:
        return {"kind": "Pick a kind."}, {}, {}
    if form.get("url") and not form.get("host"):
        split = registry.split_url(form["url"])
        form = {
            **form,
            "host": split["host"],
            "port": str(split["port"]),
            "use_ssl": "on" if split["use_ssl"] else "",
            "url_base": split["url_base"],
        }
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Give it a name."
    elif len(name) > 100:
        errors["name"] = "Keep the name under 100 characters."
    elif _name_taken(session, DownloadClient, name, row.id if row else None):
        errors["name"] = "Another entry already has this name."
    priority = _int(form.get("priority", ""), 1, 50, "Priority", errors, "priority")
    settings = dict(row.settings or {}) if row is not None else {}
    columns: dict = {}
    secrets: dict = {}
    for f in kind.fields:
        raw = form.get(f.name)
        if f.type == "secret":
            if (raw or "").strip():
                secrets[f.store] = raw.strip()
            continue
        if f.type == "bool":
            value = raw in ("on", "true", "1")
        elif f.type == "int":
            value = _int(raw or "", 1, 65535, f.label, errors, f.name)
            if value is None and f.name not in errors:
                value = f.default
        elif f.type == "select":
            allowed = [c for c, _ in f.choices]
            value = raw if raw in allowed else f.default
        else:
            value = (raw or "").strip()[:500]
        if f.type == "folder" and value:
            _check_folder(f, value, downloads_dir, errors)
        if f.required and value in ("", None):
            errors[f.name] = f"Enter the {f.label.lower()}."
        if f.store == "settings":
            settings[f.name] = value
        else:
            columns[f.store] = value
    host = str(settings.get("host") or "")
    if host and (re.search(r"[/\\\s@?#]", host) or "://" in host):
        errors["host"] = (
            "Only the host name or address; port, SSL and URL base have their own fields."
        )
    if len(columns.get("category") or "") > 100:
        errors["category"] = "Keep it under 100 characters."
    url = "" if kind.local else registry.compose_url(settings)
    if url and not errors.get("host"):
        _, problem = parse_url(url)
        if problem:
            errors["host"] = problem
    if kind.required_secret == "api_key" and _key_missing(
        session, DownloadClient, kind.kind, row, secrets.get("api_key", "")
    ):
        errors["api_key"] = "Enter the API key."
    data = {
        "kind": kind.kind,
        "protocol": kind.protocol,
        "name": name,
        "url": url,
        "priority": priority or 1,
        "enabled": form.get("enabled") == "on",
        "settings": settings,
        "category": columns.get("category") or "",
        "username": columns.get("username") or None,
    }
    return errors, data, secrets


@router.get("/settings/downloads/new", response_class=HTMLResponse)
def client_new(request: Request, kind: str = "") -> HTMLResponse:
    if kind not in sources.CLIENT_KINDS:
        groups = [
            (
                title,
                [(k.kind, k.label, k.info) for k in registry.KINDS.values() if k.protocol == proto],
            )
            for title, proto in (("Usenet", "usenet"), ("Torrents", "torrent"))
        ]
        return _render(
            request,
            "settings/_kind_picker.html",
            what="download client",
            base="/settings/downloads/new",
            groups=groups,
            intro="PeriodiCool supports these usenet and torrent download clients. Pick one, "
            "then fill in its address and settings, as in Sonarr.",
        )
    with request.app.state.sessions() as session:
        return _client_form(request, session, _client_values(None, kind))


@router.get("/settings/downloads/{row_id:int}/edit", response_class=HTMLResponse)
def client_edit(request: Request, row_id: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        row = session.get(DownloadClient, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        return _client_form(request, session, _client_values(row, row.kind), row=row)


@router.post("/settings/downloads/save", response_class=HTMLResponse)
async def client_save(request: Request) -> Response:
    lists = await form_lists(request)
    form = {k: v[-1] for k, v in lists.items()}
    with request.app.state.sessions() as session:
        row = None
        if form.get("id"):
            row = session.get(DownloadClient, int(form["id"])) if form["id"].isdigit() else None
            if row is None:
                raise HTTPException(status_code=404)
        errors, data, secrets = _parse_client(
            session, lists, row, request.app.state.settings.downloads_dir
        )
        if errors:
            values = {**form, "enabled": form.get("enabled") == "on"}
            for f in (
                registry.KINDS[form["kind"]].fields if form.get("kind") in registry.KINDS else ()
            ):
                if f.type == "bool":
                    values[f.name] = form.get(f.name) == "on"
            return _client_form(request, session, values, errors, row)
        if row is None:
            row = DownloadClient(**data, path_map=[])
            session.add(row)
        else:
            if row.kind != data["kind"]:
                raise HTTPException(status_code=400)
            for field, value in data.items():
                setattr(row, field, value)
        for column, value in secrets.items():  # an empty field keeps the saved secret
            setattr(row, column, value)
        session.commit()
        _learn(request, *secrets.values())
    return Response(status_code=204, headers={"HX-Redirect": "/settings/downloads?saved=1"})


@router.post("/settings/downloads/{row_id:int}/delete")
def client_delete(request: Request, row_id: int) -> Response:
    with request.app.state.sessions() as session:
        row = session.get(DownloadClient, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        active = session.scalar(
            select(func.count())
            .select_from(Grab)
            .where(Grab.client_id == row_id, Grab.status.in_(ACTIVE_GRAB))
        )
        if active:
            return _toast(
                f"{row.name} still has {active} PeriodiCool downloads in progress. "
                "Disable it instead, or wait until they finish.",
                "warn",
            )
        session.delete(row)
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/settings/downloads?saved=1"})


def _check_folder(f, value: str, downloads_dir: Path | None, errors: dict) -> None:
    path = Path(value)
    if not path.is_absolute():
        errors[f.name] = "Use a full path, as PeriodiCool sees it, such as /downloads/blackhole."
    elif not path.is_dir():
        errors[f.name] = f"{value} does not exist (as PeriodiCool sees it)."
    elif (
        f.name == "watch_folder"
        and downloads_dir is not None
        and not path.resolve().is_relative_to(downloads_dir.resolve())
    ):
        errors[f.name] = (
            f"Must be inside the downloads folder ({downloads_dir}); imports only read from there."
        )


def _check_client(request, session, row: DownloadClient) -> str:
    client = sources.build_client(session, row, sources.transports_from(request.app.state))
    if client is None:
        raise ClientError("the address and API key are both needed")
    kind = registry.KINDS.get(row.kind)
    if kind is not None and kind.local:
        client.downloads_dir = request.app.state.settings.downloads_dir
        client.check()
        return f"{row.name}: both folders are ready."
    version = client.check(row.category)
    label = sources.CLIENT_KINDS.get(row.kind, row.kind)
    return f"{row.name}: {label} {version} answered, and the category {row.category!r} exists."


@router.post("/settings/downloads/test")
async def client_test_form(request: Request) -> Response:
    """Test the values in the pop-up before saving. Nothing is stored."""
    lists = await form_lists(request)
    form = {k: v[-1] for k, v in lists.items()}
    with request.app.state.sessions() as session:
        stored = None
        if (form.get("id") or "").isdigit():
            stored = session.get(DownloadClient, int(form["id"]))
        if form.get("kind") not in sources.CLIENT_KINDS:
            return _toast("Pick a kind first.", "warn")
        errors, data, secrets = _parse_client(
            session, lists, stored, request.app.state.settings.downloads_dir
        )
        errors.pop("name", None)  # a test needs no unique name
        if errors:
            return _toast(next(iter(errors.values())), "warn")
        if not data["url"] and not registry.KINDS[data["kind"]].local:
            return _toast("Enter the host first.", "warn")
        probe = DownloadClient(
            id=stored.id if stored else None,
            path_map=[],
            api_key=secrets.get("api_key") or (stored.api_key if stored else None),
            password=secrets.get("password") or (stored.password if stored else None),
            **data,
        )
        _learn(request, probe.api_key, probe.password)
        try:
            text = _check_client(request, session, probe)
        except ClientError as exc:
            return _toast(f"Test failed: {exc}", "bad")
    return _toast(text)


@router.post("/settings/downloads/{row_id:int}/test")
def client_test_row(request: Request, row_id: int) -> Response:
    with request.app.state.sessions() as session:
        row = session.get(DownloadClient, row_id)
        if row is None:
            raise HTTPException(status_code=404)
        try:
            text = _check_client(request, session, row)
        except ClientError as exc:
            return _toast(f"{row.name} test failed: {exc}", "bad")
    return _toast(text)


# Remote path mappings: one list for every client, stored in each client's path_map.


def _mapping_form(request, session, values: dict, errors=None):
    clients = list(
        session.scalars(select(DownloadClient).order_by(DownloadClient.priority, DownloadClient.id))
    )
    return _render(
        request,
        "settings/_mapping_form.html",
        values=values,
        errors=errors or {},
        clients=clients,
    )


@router.get("/settings/downloads/mappings/new", response_class=HTMLResponse)
def mapping_new(request: Request) -> HTMLResponse:
    with request.app.state.sessions() as session:
        return _mapping_form(request, session, {})


@router.get("/settings/downloads/mappings/{client_id:int}/{index:int}", response_class=HTMLResponse)
def mapping_edit(request: Request, client_id: int, index: int) -> HTMLResponse:
    with request.app.state.sessions() as session:
        row = session.get(DownloadClient, client_id)
        pairs = (row.path_map or []) if row else []
        if not 0 <= index < len(pairs):
            raise HTTPException(status_code=404)
        values = {
            "client_id": client_id,
            "index": index,
            "remote": pairs[index][0],
            "local": pairs[index][1],
        }
        return _mapping_form(request, session, values)


@router.post("/settings/downloads/mappings/save", response_class=HTMLResponse)
async def mapping_save(request: Request) -> Response:
    form = await form_data(request)
    with request.app.state.sessions() as session:
        errors: dict[str, str] = {}
        target = None
        if (form.get("client_id") or "").isdigit():
            target = session.get(DownloadClient, int(form["client_id"]))
        if target is None:
            errors["client_id"] = "Pick the download client."
        remote = (form.get("remote") or "").strip()
        local = (form.get("local") or "").strip()
        if not remote:
            errors["remote"] = "Enter the folder as the download client sees it."
        if not local:
            errors["local"] = "Enter the same folder as PeriodiCool sees it."
        old = None
        if (form.get("old_client") or "").isdigit() and (form.get("old_index") or "").isdigit():
            old = (session.get(DownloadClient, int(form["old_client"])), int(form["old_index"]))
        if errors:
            values = {**form, "client_id": form.get("client_id", "")}
            if old:
                values |= {"index": form["old_index"], "client_id_was": form["old_client"]}
            return _mapping_form(request, session, values, errors)
        pair = [remote.rstrip("/") or "/", local.rstrip("/\\") or "/"]
        if old and old[0] is not None and 0 <= old[1] < len(old[0].path_map or []):
            kept = list(old[0].path_map)
            if old[0].id == target.id:
                kept[old[1]] = pair
                target.path_map = kept
            else:
                del kept[old[1]]
                old[0].path_map = kept
                target.path_map = [*(target.path_map or []), pair]
        else:
            target.path_map = [*(target.path_map or []), pair]
        session.commit()
    return Response(
        status_code=204, headers={"HX-Redirect": "/settings/downloads?saved=1#mappings"}
    )


@router.post("/settings/downloads/mappings/{client_id:int}/{index:int}/delete")
def mapping_delete(request: Request, client_id: int, index: int) -> Response:
    with request.app.state.sessions() as session:
        row = session.get(DownloadClient, client_id)
        pairs = list((row.path_map or []) if row else [])
        if not 0 <= index < len(pairs):
            raise HTTPException(status_code=404)
        del pairs[index]
        row.path_map = pairs
        session.commit()
    return Response(
        status_code=204, headers={"HX-Redirect": "/settings/downloads?saved=deleted#mappings"}
    )
