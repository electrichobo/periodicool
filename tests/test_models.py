from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from periodicool import db
from periodicool.models import (
    FormatProfile,
    Frequency,
    Issue,
    IssueStatus,
    Magazine,
    MonitorMode,
    Numbering,
)


@pytest.fixture
def session(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = db.make_engine(url)
    with db.make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _magazine(session) -> Magazine:
    profile = session.scalars(select(FormatProfile)).one()
    mag = Magazine(
        title="Fortean Times",
        frequency=Frequency.THIRTEEN_PER_YEAR,
        numbering=Numbering.ISSUE,
        format_profile=profile,
    )
    session.add(mag)
    session.flush()
    return mag


def test_defaults(session):
    mag = _magazine(session)
    session.commit()
    session.expire_all()
    mag = session.get(Magazine, mag.id)
    assert mag.aliases == [] and mag.languages == ["english"]
    assert mag.monitored and mag.monitor_mode is MonitorMode.NEW_ONLY
    assert mag.grace_days == 14
    assert mag.format_profile.name == "Any"
    assert mag.title_spec().title == "Fortean Times"


def test_issue_round_trip(session):
    mag = _magazine(session)
    session.add(
        Issue(magazine_id=mag.id, key="2023-11", cover_date=date(2023, 11, 1), issue_number=437)
    )
    session.commit()
    session.expire_all()
    issue = session.scalars(select(Issue)).one()
    assert issue.status is IssueStatus.WANTED
    assert issue.expected and issue.monitored and not issue.special
    assert issue.magazine.title == "Fortean Times"


def test_issue_key_unique_per_magazine(session):
    mag = _magazine(session)
    session.add(Issue(magazine_id=mag.id, key="2023-11", cover_date=date(2023, 11, 1)))
    session.add(Issue(magazine_id=mag.id, key="2023-11", cover_date=date(2023, 11, 1)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_magazine_removes_issue_rows(session):
    mag = _magazine(session)
    session.add(Issue(magazine_id=mag.id, key="2023-11", cover_date=date(2023, 11, 1)))
    session.commit()
    session.delete(mag)
    session.commit()
    assert session.scalars(select(Issue)).all() == []


def test_invalid_enum_rejected(session):
    with pytest.raises(StatementError, match="not among the defined enum values"):
        session.add(Magazine(title="X", frequency="fortnightly", numbering=Numbering.DATE))
        session.flush()
