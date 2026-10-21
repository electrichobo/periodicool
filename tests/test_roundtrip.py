"""PeriodiCool must read back every name it writes: render a file name for an
issue, classify it, and land on the same magazine and issue with no review."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import db
from periodicool.cli import magazine_from_dict
from periodicool.library import classify
from periodicool.models import FormatProfile, ItemAction, Magazine
from periodicool.naming import DEFAULT_TEMPLATE, render
from periodicool.overview import issue_fields
from periodicool.schedule import sync_expected_issues

EXAMPLES = Path(__file__).parent.parent / "examples" / "magazines.json"
TODAY = date(2026, 10, 21)
TEMPLATES = [
    DEFAULT_TEMPLATE,
    "{Title}/{Title} {Date}.{ext}",
    "{Title} - {Year}/{Date} - {Title}.{ext}",
]


@pytest.fixture(scope="module")
def magazines(tmp_path_factory):
    url = f"sqlite:///{(tmp_path_factory.mktemp('rt') / 'rt.db').as_posix()}"
    db.upgrade(url)
    engine = db.make_engine(url)
    with db.make_session_factory(engine)() as s:
        profile = s.scalars(select(FormatProfile)).one()
        for entry in json.loads(EXAMPLES.read_text(encoding="utf-8")):
            mag = magazine_from_dict(entry)
            mag.format_profile = profile
            mag.monitor_mode = "all"
            s.add(mag)
        s.flush()
        mags = list(s.scalars(select(Magazine)))
        cases = []
        for mag in mags:
            issues = sync_expected_issues(s, mag, TODAY)
            for issue in issues[-3:]:  # the newest few of each magazine
                cases.append((mag, issue.key, issue_fields(issue)))
        make = next(m for m in mags if m.title == "Make:")
        cases.append((make, "v95", {"year": 2026, "volume_number": 95}))
        people = next(m for m in mags if m.title == "People")
        special = {
            "year": 2026,
            "month": 7,
            "day": 7,
            "special": True,
            "cover_date": date(2026, 7, 7),
        }
        cases.append((people, "special-2026-07-07", special))
        s.expunge_all()
        yield mags, cases
    engine.dispose()


def test_every_magazine_has_cases(magazines):
    mags, cases = magazines
    assert {m.title for m in mags} <= {mag.title for mag, _, _ in cases}


@pytest.mark.parametrize("template", TEMPLATES)
def test_round_trip(magazines, template, tmp_path):
    mags, cases = magazines
    failures = []
    for mag, key, fields in cases:
        rel = render(template, mag, fields, "pdf", key=key)
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        d = classify(path, tmp_path, tmp_path, mags, template)
        if d.action is ItemAction.REVIEW or d.magazine is not mag or d.issue_key != key:
            got = d.magazine.title if d.magazine else None
            failures.append(f"{rel}: got {got} {d.issue_key} ({d.reason})")
    assert not failures, "\n".join(failures)
