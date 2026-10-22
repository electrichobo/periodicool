"""Settings tabs, Sonarr style. Indexer and client entries are in web/sources.py."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from periodicool import auth, naming_preview, preferences
from periodicool.library import get_setting
from periodicool.models import FormatProfile, Magazine
from periodicool.parser import FORMATS
from periodicool.web import sources as sources_ui
from periodicool.web.magazines import _render, _toast
from periodicool.web.security import form_data, form_lists

router = APIRouter(include_in_schema=False)

TABS = (
    ("media", "Media management"),
    ("profiles", "Profiles"),
    ("indexers", "Indexers"),
    ("downloads", "Download clients"),
    ("metadata", "Metadata"),
    ("connect", "Connect"),
    ("tags", "Tags"),
    ("general", "General"),
    ("ui", "UI"),
)
SAVED = {
    "1": "Saved.",
    "deleted": "Deleted.",
    "deleted-kept": "Deleted. Releases found through it stay in history.",
    "key": "New API key made. Update your calendar app's feed address.",
}


def _page(request, session, section: str, *, errors=None, form=None, status_code=200, **extra):
    context = {
        "tabs": TABS,
        "section": section,
        "saved": SAVED.get(request.query_params.get("saved", "")),
    }
    context["errors"] = errors or {}
    if section in preferences.SECTIONS:
        title, fields = preferences.SECTIONS[section]
        values = preferences.values(session, section)
        if form is not None:
            values = {f.name: form.get(f.name, "") for f in fields}
            for f in fields:
                if f.kind == "bool":
                    values[f.name] = form.get(f.name) == "on"
        context |= {"title": title, "fields": fields, "values": values, "errors": errors or {}}
    else:
        context["title"] = dict(TABS)[section]
    if section == "media":
        template = (form or {}).get("naming__template") or get_setting(
            session, "naming.template", preferences.SECTIONS["media"][1][0].default
        )
        settings = request.app.state.settings
        context |= {
            "tokens": naming_preview.TOKENS,
            "examples": naming_preview.examples(template),
            "paths": _paths(settings),
        }
    if section == "indexers":
        context |= sources_ui.indexer_context(session)
    if section == "downloads":
        context |= sources_ui.client_context(session)
    if section == "general":
        context["api_key"] = auth.api_key(request.app.state.settings.config_dir)
    if section == "profiles":
        context |= _profiles_context(session)
    if section == "tags":
        context["tags"] = _tag_counts(session)
    response = _render(request, "settings/page.html", active="/settings", **context, **extra)
    response.status_code = status_code
    return response


def _paths(settings) -> list[tuple[str, str, object]]:
    import shutil

    out = []
    for label, path in (
        ("Library", settings.library_dir),
        ("Downloads", settings.downloads_dir),
        ("Import folder", settings.import_dir),
    ):
        usage = None
        if path is not None and path.exists():
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                usage = None
        out.append((label, str(path) if path else "Not set", usage))
    return out


@router.get("/settings")
def settings_home() -> RedirectResponse:
    return RedirectResponse("/settings/media", status_code=307)


# Media management


@router.post("/settings/media/preview", response_class=HTMLResponse)
async def naming_examples(request: Request) -> HTMLResponse:
    form = await form_data(request)
    template = form.get("naming__template", "")
    problem = naming_preview.template_problem(template)
    return _render(
        request,
        "settings/_examples.html",
        examples=naming_preview.examples(template) if not problem else [],
        problem=problem,
    )


# General


@router.post("/settings/general/api-key")
def regenerate_key(request: Request) -> Response:
    settings = request.app.state.settings
    new = auth.regenerate_api_key(settings.config_dir)
    logs = getattr(request.app.state, "logs", None)
    if logs is not None:
        logs.redactor.secrets.append(new)
    return Response(status_code=204, headers={"HX-Redirect": "/settings/general?saved=key"})


# Profiles


def _profiles_context(session) -> dict:
    used = dict(
        session.execute(
            select(Magazine.format_profile_id, func.count()).group_by(Magazine.format_profile_id)
        ).all()
    )
    profiles = list(session.scalars(select(FormatProfile).order_by(FormatProfile.name)))
    return {"profiles": profiles, "used": used, "all_formats": FORMATS}


@router.post("/settings/profiles", response_class=HTMLResponse)
async def profile_save(request: Request) -> Response:
    lists = await form_lists(request)
    form = {k: v[-1] for k, v in lists.items()}
    name = form.get("name", "").strip()
    formats = [f for f in lists.get("formats", []) if f in FORMATS]

    # Order: each chosen format has a rank field; lower comes first.
    def rank(fmt):
        try:
            return int(form.get(f"rank_{fmt}", "9"))
        except ValueError:
            return 9

    formats.sort(key=rank)
    with request.app.state.sessions() as session:
        errors = {}
        if not name:
            errors["name"] = "Enter a name."
        if not formats:
            errors["formats"] = "Choose at least one format."
        profile_id = form.get("id", "")
        profile = session.get(FormatProfile, int(profile_id)) if profile_id.isdigit() else None
        clash = session.scalars(select(FormatProfile).where(FormatProfile.name == name)).first()
        if clash is not None and clash is not profile:
            errors["name"] = "Another profile has this name."
        if errors:
            return _page(request, session, "profiles", errors=errors, status_code=422, editing=form)
        if profile is None:
            profile = FormatProfile(name=name, formats=formats)
            session.add(profile)
        profile.name, profile.formats = name, formats
        profile.prefer_true_pdf = form.get("prefer_true_pdf") == "on"
        profile.upgrades = form.get("upgrades") == "on"
        session.commit()
    return RedirectResponse("/settings/profiles?saved=1", status_code=303)


@router.post("/settings/profiles/{profile_id:int}/delete")
def profile_delete(request: Request, profile_id: int) -> Response:
    with request.app.state.sessions() as session:
        profile = session.get(FormatProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404)
        users = session.scalar(
            select(func.count())
            .select_from(Magazine)
            .where(Magazine.format_profile_id == profile_id)
        )
        if users:
            return _toast(f"{profile.name} is used by {users} magazines. Move them first.", "warn")
        session.delete(profile)
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/settings/profiles?saved=1"})


# Tags


def _tag_counts(session) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for tags in session.scalars(select(Magazine.tags)):
        for tag in tags or []:
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[0].lower())


@router.post("/settings/tags/rename")
async def tag_rename(request: Request) -> Response:
    form = await form_data(request)
    old, new = form.get("old", "").strip(), form.get("new", "").strip()
    if not old or not new:
        return _toast("Enter the new name.", "warn")
    with request.app.state.sessions() as session:
        for mag in session.scalars(select(Magazine)):
            if old in (mag.tags or []):
                tags = [new if t == old else t for t in mag.tags]
                mag.tags = list(dict.fromkeys(tags))
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/settings/tags?saved=1"})


@router.post("/settings/tags/delete")
async def tag_delete(request: Request) -> Response:
    form = await form_data(request)
    tag = form.get("tag", "")
    with request.app.state.sessions() as session:
        for mag in session.scalars(select(Magazine)):
            if tag in (mag.tags or []):
                mag.tags = [t for t in mag.tags if t != tag]
        session.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/settings/tags?saved=1"})


# Generic tabs last, so the specific routes above match first.


@router.get("/settings/{section}", response_class=HTMLResponse)
def section_page(request: Request, section: str) -> HTMLResponse:
    if section not in dict(TABS):
        raise HTTPException(status_code=404)
    with request.app.state.sessions() as session:
        return _page(request, session, section)


@router.post("/settings/{section}", response_class=HTMLResponse)
async def section_save(request: Request, section: str) -> Response:
    if section not in preferences.SECTIONS:
        raise HTTPException(status_code=404)
    lists = await form_lists(request)
    form = {k: v[-1] for k, v in lists.items()}
    with request.app.state.sessions() as session:
        errors = preferences.save(session, section, form)
        if errors:
            session.rollback()
            return _page(request, session, section, errors=errors, form=form, status_code=422)
        session.commit()
    return RedirectResponse(f"/settings/{section}?saved=1", status_code=303)
