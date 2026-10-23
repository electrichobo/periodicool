"""Serve PeriodiCool under a URL base (for example /periodicool behind a reverse proxy).

A small ASGI wrapper: requests must start with the base, which becomes the
root_path; our own links, form actions, htmx URLs, redirects and HX headers in
the responses get the base put in front. Only attributes that hold URLs are
touched (folder paths in the folder browser are not).
"""

from __future__ import annotations

import re

URL_ATTRS = (
    "href",
    "src",
    "action",
    "hx-get",
    "hx-post",
    "hx-put",
    "hx-patch",
    "hx-delete",
    "data-open",
)
_ATTR = re.compile(r"(\s(?:" + "|".join(re.escape(a) for a in URL_ATTRS) + r')=")/(?!/)')
_HEADERS = (b"location", b"hx-redirect", b"hx-location")


def clean_base(raw: str | None) -> str:
    """ "" or "/name" (no trailing slash)."""
    raw = (raw or "").strip().strip("/")
    return f"/{raw}" if raw else ""


def rewrite_html(body: bytes, base: str) -> bytes:
    text = body.decode("utf-8", errors="surrogateescape")
    return _ATTR.sub(lambda m: m.group(1) + base + "/", text).encode(
        "utf-8", errors="surrogateescape"
    )


class UrlBase:
    def __init__(self, app, base: str) -> None:
        self.app = app
        self.base = clean_base(base)

    def __getattr__(self, name):
        # TestClient and uvicorn reach through to the FastAPI app (app.state and the like).
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not self.base:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        base = self.base
        if path == "/health":  # the container health check stays at the root
            await self.app(scope, receive, send)
            return
        if path != base and not path.startswith(base + "/"):
            await _redirect(send, base + "/") if path in ("", "/") else await _not_found(send)
            return
        # Starlette routes on the path with root_path taken off, so the path stays whole.
        scope = dict(scope)
        scope["root_path"] = scope.get("root_path", "") + base

        start: dict = {}
        chunks: list[bytes] = []

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = []
                for key, value in message.get("headers", []):
                    if (
                        key.lower() in _HEADERS
                        and value.startswith(b"/")
                        and not value.startswith(b"//")
                    ):
                        value = base.encode() + value
                    headers.append((key, value))
                message = {**message, "headers": headers}
                ctype = dict((k.lower(), v) for k, v in headers).get(b"content-type", b"")
                if ctype.startswith(b"text/html"):
                    start["message"] = message
                    return
                await send(message)
            elif message["type"] == "http.response.body" and "message" in start:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = rewrite_html(b"".join(chunks), base)
                head = start.pop("message")
                head["headers"] = [
                    (k, v) for k, v in head["headers"] if k.lower() != b"content-length"
                ] + [(b"content-length", str(len(body)).encode())]
                await send(head)
                await send({"type": "http.response.body", "body": body})
            else:
                await send(message)

        await self.app(scope, receive, wrapped)


async def _redirect(send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", location.encode()), (b"content-length", b"0")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _not_found(send) -> None:
    body = b"Not found. PeriodiCool is served under its URL base."
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
