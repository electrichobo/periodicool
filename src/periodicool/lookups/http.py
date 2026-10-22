"""A small GET client for the public metadata sources (standard library only)."""

from __future__ import annotations

import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from periodicool import __version__
from periodicool.lookups import LookupError

USER_AGENT = f"PeriodiCool/{__version__} (+https://github.com/electrichobo/periodicool)"
TIMEOUT_S = 15.0
MAX_BYTES = 2 * 1024 * 1024
MIN_INTERVAL_S = 1.0

# (url, headers, timeout) -> (status, body). Tests replace it.
Transport = Callable[[str, dict, float], tuple[int, bytes]]


def _tls_context() -> ssl.SSLContext:
    # Full verification: trusted chain and matching host name. Only Python 3.13's
    # extra strict RFC 5280 checks are relaxed, because archive.org's chain fails
    # one of them (a CA without a critical Basic Constraints flag) while browsers
    # and curl accept it.
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


def _urllib_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_tls_context()) as resp:
            return resp.status, resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


TRANSPORT: Transport | None = None  # None means real HTTP
_last: dict[str, float] = {}
_lock = threading.Lock()


def get(source: str, url: str, params: dict, accept: str = "*/*", transport=None) -> bytes:
    """GET url?params from a source, spaced at least a second from its last request."""
    full = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    send = transport or TRANSPORT or _urllib_transport
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last.get(source, 0.0))
        if wait > 0 and send is _urllib_transport:
            time.sleep(wait)
        _last[source] = time.monotonic()
    try:
        status, body = send(full, {"User-Agent": USER_AGENT, "Accept": accept}, TIMEOUT_S)
    except OSError as exc:
        raise LookupError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc}") from None
    if not 200 <= status < 300:
        raise LookupError(f"{urllib.parse.urlsplit(url).netloc} answered {status}")
    if len(body) > MAX_BYTES:
        raise LookupError("answer too large")
    return body
