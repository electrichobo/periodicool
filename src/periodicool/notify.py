"""Notifications: Sonarr's Webhook connection. Events are posted as JSON to the
addresses the user entered under Settings, Connect, and nowhere else.

Events are queued on the database session where they happen and handed to the
sender only once that session commits, so nothing is sent for work that did not
land. Sending runs on its own thread; a webhook that fails or hangs never holds up
a grab, an import or a page. Payloads never carry keys, passwords, download links
or absolute paths.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from periodicool import USER_AGENT

log = logging.getLogger(__name__)

INSTANCE_NAME = "PeriodiCool"
APPLICATION_URL_KEY = "general.application_url"
TIMEOUT_S = 10.0
MAX_HEADERS = 10
# Headers the request itself sets; a webhook entry may not replace them.
RESERVED_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection"}
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# Sonarr's event names, with magazines in place of series. Order is the form's order.
EVENTS = {
    "Grab": "On grab",
    "Download": "On import",
    "DownloadFailure": "On download failure",
    "ImportFailure": "On import failure",
    "ManualInteractionRequired": "On manual interaction required",
    "MagazineAdd": "On magazine add",
    "MagazineDelete": "On magazine delete",
    "Health": "On health issue",
    "HealthRestored": "On health restored",
}
EVENT_HELP = {
    "Grab": "A release was sent to a download client.",
    "Download": "An issue was imported into the library.",
    "DownloadFailure": "A download failed; PeriodiCool blocklists it and tries the next release.",
    "ImportFailure": "A finished download could not be imported.",
    "ManualInteractionRequired": "A download is waiting in the review queue.",
    "Health": "A health check reports a problem (and warnings, when included below).",
    "HealthRestored": "A health check that reported something is fine again.",
}


# Queueing on the session


def queue_event(session: Session, event_type: str, payload: dict) -> None:
    """Send `event_type` once this session commits. Nothing is sent on rollback."""
    session.info.setdefault("notify", []).append((event_type, payload))


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    events = session.info.pop("notify", None)
    if events and _sender is not None:
        for event_type, payload in events:
            _sender.submit(event_type, payload)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("notify", None)


# Payloads


def base(session: Session, event_type: str) -> dict:
    from periodicool.library import get_setting

    return {
        "eventType": event_type,
        "instanceName": INSTANCE_NAME,
        "applicationUrl": str(get_setting(session, APPLICATION_URL_KEY, "") or ""),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def magazine_info(mag) -> dict | None:
    if mag is None:
        return None
    frequency = getattr(mag.frequency, "value", mag.frequency)
    return {"id": mag.id, "title": mag.title, "issn": mag.issn, "frequency": frequency}


def issue_info(issue, key: str | None = None) -> dict | None:
    if issue is None:
        return {"key": key} if key else None
    return {
        "key": issue.key,
        "coverDate": issue.cover_date.isoformat() if issue.cover_date else None,
        "issueNumber": issue.issue_number,
        "volumeNumber": issue.volume_number,
        "special": bool(issue.special),
    }


def release_info(session: Session, g) -> dict:
    from periodicool.models import Indexer, Release

    release = session.get(Release, g.release_id) if g.release_id else None
    entry = session.get(Indexer, release.source_id) if release and release.source_id else None
    return {
        "title": g.title,
        "indexer": (release.indexer if release else None) or (entry.name if entry else None),
        "size": release.size if release else g.size_bytes,
        "protocol": g.protocol,
        "downloadClient": g.client_label,
        "downloadId": g.job_id,
    }


def for_history(session: Session, history_event, magazine_id, issue_key, grab=None, data=None):
    """The webhook event for a history row, or None when it has none."""
    from periodicool.models import HistoryEvent, Issue, Magazine

    event_type = {
        HistoryEvent.GRABBED: "Grab",
        HistoryEvent.IMPORTED: "Download",
        HistoryEvent.DOWNLOAD_FAILED: "DownloadFailure",
        HistoryEvent.IMPORT_FAILED: "ImportFailure",
        HistoryEvent.IMPORT_REVIEW: "ManualInteractionRequired",
    }.get(history_event)
    if event_type is None or not _anyone_listening(session, event_type):
        return
    mag = session.get(Magazine, magazine_id) if magazine_id else None
    issue = None
    if mag is not None and issue_key:
        issue = session.scalars(
            select(Issue).where(Issue.magazine_id == mag.id, Issue.key == issue_key)
        ).first()
    payload = base(session, event_type) | {
        "magazine": magazine_info(mag),
        "issue": issue_info(issue, issue_key),
    }
    if grab is not None:
        payload["release"] = release_info(session, grab)
    data = data or {}
    if data.get("reason"):
        payload["reason"] = str(data["reason"])[:1000]
    if event_type == "Download" and issue is not None and issue.file_path:
        payload["file"] = {
            "path": issue.file_path,  # relative to the library
            "format": issue.file_format,
            "size": issue.file_size,
        }
    queue_event(session, event_type, payload)


def magazine_event(session: Session, event_type: str, mag) -> None:
    if _anyone_listening(session, event_type):
        queue_event(
            session, event_type, base(session, event_type) | {"magazine": magazine_info(mag)}
        )


def health_event(session: Session, restored: bool, check) -> None:
    event_type = "HealthRestored" if restored else "Health"
    if _anyone_listening(session, event_type):
        queue_event(
            session,
            event_type,
            base(session, event_type)
            | {
                "health": {
                    "name": check["name"],
                    "level": check["kind"],
                    "message": check["message"],
                    "fix": check.get("fix", ""),
                }
            },
        )


def _anyone_listening(session: Session, event_type: str) -> bool:
    """Skip building a payload when no enabled connection wants the event."""
    from periodicool.models import Notification

    return any(
        event_type in (events or [])
        for events in session.scalars(
            select(Notification.events).where(Notification.enabled.is_(True))
        )
    )


# Sending


def _no_redirects():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None  # never follow: auth and headers must not reach another host

    return urllib.request.build_opener(NoRedirect)


def _urllib_transport(method: str, url: str, headers: dict, body: bytes, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _no_redirects().open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def deliver(row, event_type: str, payload: dict, transport=None) -> tuple[bool, str]:
    """Send one event to one webhook entry. Returns (sent, what happened)."""
    settings = row.settings or {}
    method = "PUT" if settings.get("method") == "PUT" else "POST"
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    for name, value in settings.get("headers") or []:
        if name and name.lower() not in RESERVED_HEADERS:
            headers[name] = value
    if row.username or row.password:
        token = base64.b64encode(f"{row.username or ''}:{row.password or ''}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    body = json.dumps(payload).encode()
    try:
        status = (transport or _urllib_transport)(method, row.url, headers, body, TIMEOUT_S)
    except (OSError, ValueError) as exc:
        return False, f"cannot reach the webhook: {_redact(row, exc)}"
    if 200 <= status < 300:
        return True, f"answered {status}"
    if 300 <= status < 400:
        return False, f"answered {status} (a redirect; PeriodiCool does not follow them)"
    return False, f"answered {status}"


def _redact(row, text) -> str:
    text = str(text)
    secrets = [row.password] + [v for _, v in (row.settings or {}).get("headers") or []]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "REDACTED")
    return text


def send_to_all(sessions: Callable[[], Session], event_type: str, payload: dict, transport=None):
    """Send to every enabled webhook that wants the event, and note each result."""
    from periodicool.models import Notification

    with sessions() as session:
        rows = session.scalars(
            select(Notification).where(Notification.enabled.is_(True)).order_by(Notification.id)
        ).all()
        for row in rows:
            if event_type not in (row.events or []):
                continue
            level = (payload.get("health") or {}).get("level")
            if (
                event_type == "Health"
                and level == "warn"
                and not (row.settings or {}).get("include_health_warnings")
            ):
                continue
            sent, what = deliver(row, event_type, payload, transport)
            record_result(row, sent, what)
            if sent:
                log.info("Webhook %s: %s %s", row.name, event_type, what)
            else:
                log.warning("Webhook %s: %s not sent, %s", row.name, event_type, what)
        session.commit()


def record_result(row, sent: bool, what: str) -> None:
    row.last_sent_at = datetime.now(UTC)
    row.last_status = "sent" if sent else "failed"
    row.last_error = None if sent else what[:500]


class Sender:
    """One thread that sends queued events in order. `inline` sends at once (CLI)."""

    def __init__(self, sessions, transport=None, inline: bool = False) -> None:
        self.sessions, self.transport, self.inline = sessions, transport, inline
        self._queue: queue.Queue = queue.Queue()
        self._thread = None
        if not inline:
            self._thread = threading.Thread(
                target=self._run, name="periodicool-notify", daemon=True
            )
            self._thread.start()

    def submit(self, event_type: str, payload: dict) -> None:
        if self.inline:
            self._send(event_type, payload)
        else:
            self._queue.put((event_type, payload))

    def _send(self, event_type: str, payload: dict) -> None:
        try:
            send_to_all(self.sessions, event_type, payload, self.transport)
        except Exception:  # noqa: BLE001  a webhook must never take anything down
            log.exception("Webhook sending failed for %s", event_type)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if item[0] == DRAIN:
                item[1].set()
                continue
            self._send(*item)

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout)

    def drain(self, timeout: float = 5.0) -> None:
        """Wait until everything queued so far is sent (tests)."""
        if self.inline:
            return
        done = threading.Event()
        self._queue.put((DRAIN, done))
        done.wait(timeout)


DRAIN = object()  # a queue marker: everything before it has been sent
_sender: Sender | None = None


def start(sessions, transport=None, inline: bool = False) -> Sender:
    global _sender
    stop()
    _sender = Sender(sessions, transport, inline)
    return _sender


def stop() -> None:
    global _sender
    if _sender is not None:
        _sender.stop()
    _sender = None
