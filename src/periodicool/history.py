"""History: one row per thing that happened to a magazine's issues (Sonarr's History)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from periodicool import notify
from periodicool.models import Grab, History, HistoryEvent


def record(
    session: Session,
    event: HistoryEvent,
    *,
    magazine_id: int | None = None,
    issue_key: str | None = None,
    source_title: str | None = None,
    grab_id: int | None = None,
    grab: Grab | None = None,
    **data,
) -> History:
    """Add a history row. Events that webhooks know (grab, import, failures, review)
    are also queued for them, sent once the session commits."""
    row = History(
        event=event,
        magazine_id=magazine_id,
        issue_key=issue_key,
        source_title=source_title[:1000] if source_title else None,
        grab_id=grab_id,
        data={k: v for k, v in data.items() if v is not None},
    )
    session.add(row)
    notify.for_history(session, event, magazine_id, issue_key, grab, data)
    return row


def record_grab(session: Session, event: HistoryEvent, g: Grab, **data) -> History:
    return record(
        session,
        event,
        magazine_id=g.magazine_id,
        issue_key=g.issue_key,
        source_title=g.title,
        grab_id=g.id,
        grab=g,
        client=g.client,
        job_id=g.job_id,
        **data,
    )
