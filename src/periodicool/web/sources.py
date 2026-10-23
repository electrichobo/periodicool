"""Settings, Indexers and Download clients: the lists, their pop-up forms and tests.

Keys and passwords are write-only: the form never carries the stored value, and an
empty key field keeps it.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from periodicool import sources
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


def _indexer_values(row: Indexer | None, kind: str) -> dict:
    if row is None:
        return {
            "kind": kind,
            "name": sources.INDEXER_KINDS.get(kind, ""),
            "priority": 25,
            "enabled": True,
            "categories": "",
            "daily_cap": "",
            "grab_cap": "",
            "seed_ratio": "",
            "seed_time_min": "",
            "min_seeders": "",
        }
    settings = row.settings or {}
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "url": row.url,
        "priority": row.priority,
        "enabled": row.enabled,
        "categories": ", ".join(map(str, row.categories or [])),
        "daily_cap": settings.get("daily_cap") or "",
        "grab_cap": settings.get("grab_cap") or "",
        "seed_ratio": _blank(settings.get("seed_ratio")),
        "seed_time_min": _blank(settings.get("seed_time_min")),
        "min_seeders": _blank(settings.get("min_seeders")),
    }


def _blank(value) -> str:
    return "" if value is None else f"{value:g}" if isinstance(value, float) else str(value)


def _indexer_form(request, session, values: dict, errors=None, row=None):
    return _render(
        request,
        "settings/_indexer_form.html",
        values=values,
        errors=errors or {},
        has_key=bool(row is not None and sources.indexer_key(session, row)),
        env_fallback=_uses_env(session, row, sources.indexer_key),
        kinds=sources.INDEXER_KINDS,
        key_help=KEY_HELP,
    )


def _parse_indexer(session: Session, form: dict, row: Indexer | None) -> tuple[dict, dict]:
    errors, kind, name, url, priority = _common(
        session, Indexer, form, row.id if row else None, sources.INDEXER_KINDS
    )
    categories = []
    for part in (form.get("categories") or "").replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            errors["categories"] = "Categories are numbers separated by commas, such as 7010."
            break
        categories.append(int(part))
    extra = {}
    if kind in ("prowlarr", "torznab"):
        # Seed goals and minimum seeders for torrents found through this entry.
        ratio = (form.get("seed_ratio") or "").strip()
        if ratio:
            try:
                extra["seed_ratio"] = float(ratio)
                if not 0 <= extra["seed_ratio"] <= 100:
                    raise ValueError
            except ValueError:
                errors["seed_ratio"] = "The seed ratio is a number from 0 to 100, such as 1.5."
                extra.pop("seed_ratio", None)
        for field, label, high in (
            ("seed_time_min", "Seed time", 525_600),
            ("min_seeders", "Minimum seeders", 10_000),
        ):
            value = _int(form.get(field, ""), 0, high, label, errors, field)
            if value is not None:
                extra[field] = value
    if kind in ("newznab", "torznab"):
        for field, label in (("daily_cap", "Daily API limit"), ("grab_cap", "Daily grab limit")):
            value = _int(form.get(field, ""), 1, 100_000, label, errors, field)
            if value:
                extra[field] = value
    key = (form.get("api_key") or "").strip()
    if _key_missing(session, Indexer, kind, row, key):
        errors["api_key"] = "Enter the API key."
    return errors, {
        "kind": kind,
        "protocol": "torrent" if kind == "torznab" else "usenet",
        "name": name,
        "url": url,
        "priority": priority or 25,
        "enabled": form.get("enabled") == "on",
        "categories": categories,
        "settings": extra,
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
def indexer_new(request: Request, kind: str = "") -> HTMLResponse:
    if kind not in sources.INDEXER_KINDS:
        return _render(
            request,
            "settings/_kind_picker.html",
            what="indexer",
            base="/settings/indexers/new",
            kinds=[
                ("prowlarr", "Prowlarr", "Searches every usenet indexer you set up in Prowlarr."),
                ("newznab", "Newznab", "One usenet indexer, added directly with its own API key."),
                (
                    "torznab",
                    "Torznab",
                    "One torrent indexer (or Jackett), added directly. Needs a torrent client.",
                ),
            ],
        )
    with request.app.state.sessions() as session:
        return _indexer_form(request, session, _indexer_values(None, kind))


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
    with request.app.state.sessions() as session:
        row = None
        if form.get("id"):
            row = session.get(Indexer, int(form["id"])) if form["id"].isdigit() else None
            if row is None:
                raise HTTPException(status_code=404)
        errors, data = _parse_indexer(session, form, row)
        if errors:
            values = {**form, "enabled": form.get("enabled") == "on"}
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
        probe = Indexer(
            id=stored.id if stored else None,
            name=(form.get("name") or kind).strip(),
            kind=kind,
            url=url,
            api_key=(form.get("api_key") or "").strip() or (stored.api_key if stored else None),
            settings={},
            categories=[],
        )
        _learn(request, probe.api_key)
        try:
            text = _test_indexer(request, session, probe)
        except ProwlarrError as exc:
            session.rollback()
            return _toast(f"Test failed: {exc}", "bad")
        session.commit()
    return _toast(text)


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
        rows.append(
            {
                "row": row,
                "kind_label": sources.CLIENT_KINDS.get(row.kind, row.kind),
                "has_key": bool(sources.client_key(session, row)),
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
    if row is None:
        return {
            "kind": kind,
            "name": sources.CLIENT_KINDS.get(kind, ""),
            "priority": 1,
            "enabled": True,
            "category": "",
            "username": "",
        }
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "url": row.url,
        "priority": row.priority,
        "enabled": row.enabled,
        "category": row.category,
        "username": row.username or "",
    }


def _client_form(request, session, values: dict, errors=None, row=None):
    return _render(
        request,
        "settings/_client_form.html",
        values=values,
        errors=errors or {},
        has_key=bool(row is not None and sources.client_key(session, row)),
        env_fallback=_uses_env(session, row, sources.client_key),
        kinds=sources.CLIENT_KINDS,
        key_help=KEY_HELP,
    )


def _parse_client(session, lists: dict, row: DownloadClient | None) -> tuple[dict, dict]:
    form = {k: v[-1] for k, v in lists.items()}
    errors, kind, name, url, priority = _common(
        session, DownloadClient, form, row.id if row else None, sources.CLIENT_KINDS
    )
    category = (form.get("category") or "").strip()
    if not category:
        errors["category"] = "PeriodiCool needs its own category. Create it in the client first."
    elif len(category) > 100:
        errors["category"] = "Keep the category under 100 characters."
    key = (form.get("api_key") or "").strip()
    # qBittorrent: the secret field is its password, and both may be empty when
    # qBittorrent skips login for PeriodiCool's address.
    if kind != "qbittorrent" and _key_missing(session, DownloadClient, kind, row, key):
        errors["api_key"] = "Enter the API key."
    return errors, {
        "kind": kind,
        "protocol": sources.CLIENT_PROTOCOLS.get(kind, "usenet"),
        "name": name,
        "url": url,
        "priority": priority or 1,
        "enabled": form.get("enabled") == "on",
        "category": category,
        "username": (form.get("username") or "").strip()[:200] or None,
        "api_key": key,
    }


@router.get("/settings/downloads/new", response_class=HTMLResponse)
def client_new(request: Request, kind: str = "") -> HTMLResponse:
    if kind not in sources.CLIENT_KINDS:
        return _render(
            request,
            "settings/_kind_picker.html",
            what="download client",
            base="/settings/downloads/new",
            kinds=[
                ("sabnzbd", "SABnzbd", "Usenet downloads. NZBGet comes later."),
                (
                    "qbittorrent",
                    "qBittorrent",
                    "Torrents. Files are hard-linked or copied into the library, never moved, "
                    "so they keep seeding.",
                ),
            ],
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
        errors, data = _parse_client(session, lists, row)
        if errors:
            values = {**form, "enabled": form.get("enabled") == "on"}
            return _client_form(request, session, values, errors, row)
        key = data.pop("api_key")
        secret = "password" if data["kind"] == "qbittorrent" else "api_key"
        if row is None:
            session.add(DownloadClient(**data, path_map=[], **{secret: key or None}))
        else:
            if row.kind != data["kind"]:
                raise HTTPException(status_code=400)
            for field, value in data.items():
                setattr(row, field, value)
            if key:
                setattr(row, secret, key)
        session.commit()
        _learn(request, key)
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


def _check_client(request, session, row: DownloadClient) -> str:
    client = sources.build_client(session, row, sources.transports_from(request.app.state))
    if client is None:
        raise ClientError("the address and API key are both needed")
    version = client.check(row.category)
    kind = sources.CLIENT_KINDS.get(row.kind, row.kind)
    return f"{row.name}: {kind} {version} answered, and the category {row.category!r} exists."


@router.post("/settings/downloads/test")
async def client_test_form(request: Request) -> Response:
    """Test the values in the pop-up before saving. Nothing is stored."""
    form = await form_data(request)
    with request.app.state.sessions() as session:
        stored = None
        if (form.get("id") or "").isdigit():
            stored = session.get(DownloadClient, int(form["id"]))
        url, problem = parse_url(form.get("url", ""))
        if problem or not url:
            return _toast(problem or "Enter the address first.", "warn")
        if form.get("kind") not in sources.CLIENT_KINDS:
            return _toast("Pick a kind first.", "warn")
        probe = DownloadClient(
            id=stored.id if stored else None,
            name=(form.get("name") or "SABnzbd").strip(),
            kind=form["kind"],
            url=url,
            api_key=(form.get("api_key") or "").strip() or (stored.api_key if stored else None),
            category=(form.get("category") or "").strip(),
            path_map=[],
        )
        if probe.kind == "qbittorrent":
            probe.username = (form.get("username") or "").strip()
            probe.password = (form.get("api_key") or "").strip() or (
                stored.password if stored else None
            )
            probe.api_key = None
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
