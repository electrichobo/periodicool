"""Web auth: the login gate, CSRF check, form parsing, and setup/login/logout pages."""

from __future__ import annotations

import secrets
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from periodicool import auth

# Paths anyone can reach. The calendar feed checks its own API key.
PUBLIC_PATHS = frozenset({"/health", "/login", "/setup", "/feed/calendar.ics"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

router = APIRouter(include_in_schema=False)


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def route_path(request: Request) -> str:
    """The path without the URL base, as the routes see it."""
    path, base = request.url.path, request.scope.get("root_path", "")
    return (path[len(base) :] or "/") if base and path.startswith(base) else path


def _is_https(request: Request, trust_proxy: bool) -> bool:
    if request.url.scheme == "https":
        return True
    return trust_proxy and request.headers.get("x-forwarded-proto", "").lower() == "https"


async def auth_middleware(request: Request, call_next):
    """Work out who is asking, give the page a CSRF token, and send strangers to login."""
    state = request.app.state
    token = request.cookies.get(auth.SESSION_COOKIE)
    with state.sessions() as db:
        cfg = auth.auth_config(db)
        user = auth.session_user(db, token)
        has_user = user is not None or auth.has_user(db)
        db.commit()
    peer = request.client.host if request.client else None
    address = auth.client_address(peer, request.headers.get("x-forwarded-for"), cfg.trust_proxy)
    allowed = (
        user is not None or cfg.mode == "off" or (cfg.mode == "local" and auth.is_local(address))
    )

    new_anon = None
    if user is not None:
        basis = token
    else:
        basis = request.cookies.get(auth.ANON_COOKIE)
        if not basis:
            basis = new_anon = secrets.token_urlsafe(24)

    request.state.user = user
    request.state.auth = cfg
    request.state.address = address
    request.state.https = _is_https(request, cfg.trust_proxy)
    request.state.csrf_basis = basis
    request.state.csrf_token = auth.csrf_token(state.secret, basis)

    if allowed or is_public(route_path(request)):
        response = await call_next(request)
    else:
        if cfg.mode == "required" and not has_user:
            target = "/setup"
        else:
            here = route_path(request) + (f"?{request.url.query}" if request.url.query else "")
            target = "/login?next=" + quote(here, safe="")
        if request.headers.get("hx-request"):
            response = Response(status_code=401, headers={"HX-Redirect": target})
        elif request.method in SAFE_METHODS:
            response = RedirectResponse(target, status_code=303)
        else:
            response = Response("Login required", status_code=401)
    if new_anon:
        response.set_cookie(
            auth.ANON_COOKIE,
            new_anon,
            httponly=True,
            samesite="lax",
            secure=request.state.https,
        )
    return response


async def form_data(request: Request) -> dict[str, str]:
    """URL-encoded form fields. Keeps the last value of a repeated name."""
    if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
        return {}
    body = (await request.body()).decode("utf-8", errors="replace")
    return dict(parse_qsl(body, keep_blank_values=True))


async def form_lists(request: Request) -> dict[str, list[str]]:
    """URL-encoded form fields with every value of repeated names (checkbox lists)."""
    if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
        return {}
    body = (await request.body()).decode("utf-8", errors="replace")
    out: dict[str, list[str]] = {}
    for key, value in parse_qsl(body, keep_blank_values=True):
        out.setdefault(key, []).append(value)
    return out


async def csrf_guard(request: Request) -> None:
    """App-wide dependency: every POST, PUT, PATCH and DELETE needs a valid CSRF token."""
    if request.method in SAFE_METHODS:
        return
    given = request.headers.get("x-csrf-token") or (await form_data(request)).get("csrf_token")
    basis = getattr(request.state, "csrf_basis", None)
    if not auth.csrf_ok(request.app.state.secret, basis, given):
        raise HTTPException(status_code=403, detail="Missing or wrong CSRF token. Reload the page.")


def _safe_next(target: str | None) -> str:
    # Only same-site paths: "/x" but never "//host" or "/\host".
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.state.https,
    )


def _page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    from periodicool.web import render

    response = render(request, name, nav=False, **context)
    response.status_code = status_code
    return response


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request) -> Response:
    with request.app.state.sessions() as db:
        if auth.has_user(db):
            return RedirectResponse("/login", status_code=303)
    return _page(request, "setup.html", username="admin")


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request) -> Response:
    form = await form_data(request)
    username = form.get("username", "").strip()
    password, confirm = form.get("password", ""), form.get("confirm", "")
    error = auth.password_problem(password, confirm) if username else "Enter a username."
    with request.app.state.sessions() as db:
        if auth.has_user(db):
            return RedirectResponse("/login", status_code=303)
        if error:
            return _page(request, "setup.html", 422, username=username, error=error)
        user = auth.create_user(db, username, password)
        token = auth.start_session(db, user)
        db.commit()
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, token)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    if request.state.user is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    with request.app.state.sessions() as db:
        if not auth.has_user(db):
            return RedirectResponse("/setup", status_code=303)
    return _page(request, "login.html", next=_safe_next(next), username="")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    form = await form_data(request)
    username, password = form.get("username", ""), form.get("password", "")
    target = _safe_next(form.get("next"))
    throttle: auth.LoginThrottle = request.app.state.login_throttle
    address = request.state.address
    wait = throttle.wait_left(address)
    if wait > 0:
        error = f"Too many failed logins. Try again in {int(wait) + 1} seconds."
        return _page(request, "login.html", 429, next=target, username=username, error=error)
    with request.app.state.sessions() as db:
        user = auth.authenticate(db, username, password)
        if user is None:
            throttle.failed(address)
            error = "Wrong username or password."
            return _page(request, "login.html", 401, next=target, username=username, error=error)
        throttle.succeeded(address)
        token = auth.start_session(db, user)
        db.commit()
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(request, response, token)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    with request.app.state.sessions() as db:
        auth.end_session(db, request.cookies.get(auth.SESSION_COOKIE))
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response
