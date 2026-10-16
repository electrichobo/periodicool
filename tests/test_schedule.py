"""Expected-issue generation, sync, missing status and release identification."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import db
from periodicool.matching import match_title
from periodicool.models import Frequency, Issue, IssueStatus, Magazine, MonitorMode, Numbering
from periodicool.parser import parse_release
from periodicool.schedule import (
    MISSING,
    effective_status,
    expected_issues,
    generation_range,
    identify,
    sync_expected_issues,
)

from dev_magazines import (
    AMAZONAS,
    AMERICAN_CINEMATOGRAPHER,
    BY_FIXTURE,
    FORTEAN_TIMES,
    HACKER_QUARTERLY,
    LINUX_MAGAZINE,
    MAKE,
    PEOPLE,
    TABLETOP_GAMING,
)

TODAY = date(2026, 10, 16)


def _keys(mag, start, end):
    return [e.key for e in expected_issues(mag, start, end)]


# Cadences


def test_monthly_by_date():
    assert _keys(AMERICAN_CINEMATOGRAPHER, date(2026, 11, 1), date(2027, 2, 1)) == [
        "2026-11",
        "2026-12",
        "2027-01",
        "2027-02",
    ]


def test_monthly_numbered_steps_both_ways():
    issues = expected_issues(LINUX_MAGAZINE, date(2023, 10, 1), date(2023, 12, 1))
    assert [(e.key, e.issue_number) for e in issues] == [
        ("n275", 275),
        ("n276", 276),
        ("n277", 277),
    ]
    later = expected_issues(LINUX_MAGAZINE, date(2026, 12, 1), date(2026, 12, 31))
    assert [e.issue_number for e in later] == [313]


def test_bimonthly_pairs_follow_anchor():
    issues = expected_issues(AMAZONAS, date(2017, 1, 1), date(2017, 12, 31))
    assert [(e.month, e.month_end) for e in issues] == [
        (1, 2),
        (3, 4),
        (5, 6),
        (7, 8),
        (9, 10),
        (11, 12),
    ]


def test_quarterly():
    mag = Magazine(
        title="Q",
        frequency=Frequency.QUARTERLY,
        numbering=Numbering.DATE,
        anchor_date=date(2020, 2, 1),
    )
    assert _keys(mag, date(2024, 1, 1), date(2024, 12, 31)) == [
        "2024-02",
        "2024-05",
        "2024-08",
        "2024-11",
    ]


def test_seasonal():
    issues = expected_issues(HACKER_QUARTERLY, date(2015, 1, 1), date(2015, 12, 31))
    assert [e.key for e in issues] == ["2015-spring", "2015-summer", "2015-autumn", "2015-winter"]
    assert [e.cover_date.month for e in issues] == [3, 6, 9, 12]


def test_weekly_on_publish_day():
    keys = _keys(PEOPLE, date(2026, 10, 1), date(2026, 10, 31))
    assert keys == ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"]
    assert all(date.fromisoformat(k).weekday() == 0 for k in keys)


def test_biweekly():
    mag = Magazine(
        title="B",
        frequency=Frequency.BIWEEKLY,
        numbering=Numbering.DATE,
        anchor_date=date(2026, 1, 2),
    )
    assert _keys(mag, date(2026, 1, 1), date(2026, 2, 1)) == [
        "2026-01-02",
        "2026-01-16",
        "2026-01-30",
    ]


def test_thirteen_per_year_is_monthly_without_numbers():
    issues = expected_issues(FORTEAN_TIMES, date(2026, 9, 1), date(2026, 10, 31))
    assert [(e.key, e.issue_number) for e in issues] == [("2026-09", None), ("2026-10", None)]


def test_irregular_generates_nothing():
    assert expected_issues(MAKE, date(2000, 1, 1), date(2030, 1, 1)) == []
    assert generation_range(MAKE, TODAY) is None


def test_generation_is_capped():
    mag = Magazine(
        title="W",
        frequency=Frequency.WEEKLY,
        numbering=Numbering.DATE,
        anchor_date=date(2000, 1, 3),
    )
    assert len(expected_issues(mag, date(1900, 1, 1), date(2100, 1, 1))) == 3000


# Ranges and monitor modes


def _with(mag, **kw):
    copy = Magazine(
        **{
            c: getattr(mag, c)
            for c in (
                "title",
                "frequency",
                "numbering",
                "anchor_date",
                "anchor_number",
                "schedule_start",
                "publish_weekday",
                "grace_days",
                "monitored",
                "monitor_mode",
                "monitor_from_year",
            )
        }
    )
    for k, v in kw.items():
        setattr(copy, k, v)
    return copy


def test_new_only_is_current_and_next_issue():
    assert generation_range(LINUX_MAGAZINE, TODAY) == (date(2026, 10, 1), date(2026, 11, 1))
    assert generation_range(PEOPLE, TODAY) == (date(2026, 10, 12), date(2026, 10, 19))


def test_from_year():
    mag = _with(LINUX_MAGAZINE, monitor_mode=MonitorMode.FROM_YEAR, monitor_from_year=2025)
    start, end = generation_range(mag, TODAY)
    assert start == date(2025, 1, 1) and end == date(2026, 11, 1)
    issues = expected_issues(mag, start, end)
    assert issues[0].issue_number == 290 and issues[-1].issue_number == 312


def test_all_stops_at_schedule_start():
    mag = _with(TABLETOP_GAMING, monitor_mode=MonitorMode.ALL)
    start, _ = generation_range(mag, TODAY)
    assert start == date(2018, 1, 1)
    early = _with(TABLETOP_GAMING, monitor_mode=MonitorMode.FROM_YEAR, monitor_from_year=2015)
    assert generation_range(early, TODAY)[0] == date(2018, 1, 1)


# Database sync and missing status


@pytest.fixture
def session(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = db.make_engine(url)
    with db.make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _saved(session, mag):
    saved = _with(mag, monitor_mode=MonitorMode.FROM_YEAR, monitor_from_year=2026)
    session.add(saved)
    session.flush()
    return saved


def test_sync_inserts_then_is_a_no_op(session):
    mag = _saved(session, LINUX_MAGAZINE)
    created = sync_expected_issues(session, mag, TODAY)
    assert [i.key for i in created] == [f"n{n}" for n in range(302, 313)]
    assert sync_expected_issues(session, mag, TODAY) == []
    assert len(session.scalars(select(Issue)).all()) == 11


def test_sync_never_changes_existing_rows(session):
    mag = _saved(session, LINUX_MAGAZINE)
    session.add(
        Issue(
            magazine_id=mag.id,
            key="n305",
            cover_date=date(2026, 4, 1),
            issue_number=305,
            expected=False,
            status=IssueStatus.IMPORTED,
            file_path="/library/x.pdf",
        )
    )
    session.flush()
    sync_expected_issues(session, mag, TODAY)
    kept = session.scalars(select(Issue).where(Issue.key == "n305")).one()
    assert kept.status is IssueStatus.IMPORTED and kept.file_path == "/library/x.pdf"
    assert not kept.expected


def test_missing_after_grace(session):
    mag = _saved(session, LINUX_MAGAZINE)
    sync_expected_issues(session, mag, TODAY)
    by_key = {i.key: i for i in session.scalars(select(Issue))}
    october = by_key["n311"]  # cover 2026-10-01, grace 14 days
    assert effective_status(october, mag, date(2026, 10, 15)) == "wanted"
    assert effective_status(october, mag, date(2026, 10, 16)) == MISSING
    october.monitored = False
    assert effective_status(october, mag, TODAY) == "wanted"
    october.monitored = True
    october.status = IssueStatus.GRABBED
    assert effective_status(october, mag, TODAY) == "grabbed"


# Release to issue, from real fixture names


def _id(mag, name):
    return identify(parse_release(name), mag)


@pytest.mark.parametrize(
    ("mag", "name", "key", "extra"),
    [
        (
            AMERICAN_CINEMATOGRAPHER,
            "American.Cinematographer.TruePDF-September.2007",
            "2007-09",
            {},
        ),
        (AMAZONAS, "Amazonas.TruePDF-July.August.2017", "2017-07", {"month_end": 8}),
        (HACKER_QUARTERLY, "2600.Magazine.The.Hacker.Quarterly-Winter.2015", "2015-winter", {}),
        (LINUX_MAGAZINE, "Linux.Magazine.USA.Issue.276-November.2023", "n276", {}),
        (LINUX_MAGAZINE, "Linux.Magazine.No.261.2022.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE", "n261", {}),
        (LINUX_MAGAZINE, "Linux.Magazine.USA-April.2023", "n269", {}),
        (MAKE, "Make.No.98.2026.RETAIL.MAGAZINE.eBook-21A1", "v98", {"volume_number": 98}),
        (MAKE, "MAKE Magazine vol 05", "v5", {}),
        (
            FORTEAN_TIMES,
            "Fortean.Times.Issue.351.TruePDF-March.2017",
            "2017-03",
            {"issue_number": 351},
        ),
        (FORTEAN_TIMES, "Fortean.Times-August.2022", "2022-08", {"issue_number": None}),
        (TABLETOP_GAMING, "Tabletop.Gaming.Issue.51-February.2021", "n51", {}),
        (TABLETOP_GAMING, "Tabletop.Gaming-25.November.2022", "n72", {}),
        (TABLETOP_GAMING, "Tabletop.Gaming.Issue.2.TruePDF-Autumn.2015", "n2", {}),
        (PEOPLE, "People.USA.TruePDF-22.August.2016", "2016-08-22", {}),
        (PEOPLE, "People.USA.TruePDF-04.December.2017", "2017-12-04", {}),
        (PEOPLE, "People.Special.Edition-07.July.2023", "special-2023-07-07", {"special": True}),
    ],
)
def test_identify(mag, name, key, extra):
    result = _id(mag, name)
    assert result.key == key and result.confident, result.reasons
    for field, value in extra.items():
        assert result.fields.get(field) == value


def test_weekly_on_sale_date_snaps_to_issue_date():
    sunday = _id(PEOPLE, "People.USA.TruePDF-03.December.2017")
    assert sunday.key == "2017-12-04" and sunday.confident
    assert any("moved" in n for n in sunday.notes)


@pytest.mark.parametrize(
    ("mag", "name", "reason"),
    [
        (LINUX_MAGAZINE, "Linux Magazine #04 (2024) - Jubileumeditie 25 jaar", "does not fit"),
        (LINUX_MAGAZINE, "Linux Magazine #03 (2026)", "does not fit 2026"),
        (MAKE, "Make.Magazine-August.September.2015", "no issue number"),
        (
            FORTEAN_TIMES,
            "Fortean.Times.No.416.2022.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE",
            "without a month",
        ),
        (TABLETOP_GAMING, "Tabletop.Gaming.TruePDF-December.2016", "no issue number"),
        (PEOPLE, "People.Specials-Yearbook.2023.[2023]", "special without a month"),
    ],
)
def test_identify_flags(mag, name, reason):
    result = _id(mag, name)
    assert not result.confident
    assert any(reason in r for r in result.reasons), result.reasons


def test_every_confident_fixture_name_identifies():
    """Confident matched names map to a key, except known date-only and number-only cases."""
    allowed = ("no issue number", "number without a month", "does not fit")
    for path in sorted((Path(__file__).parent / "fixtures" / "releases").glob("*.json")):
        mag = BY_FIXTURE[path.stem]
        for r in json.loads(path.read_text(encoding="utf-8"))["releases"]:
            parsed = parse_release(r["title"])
            if parsed.reasons or not match_title(parsed, mag.title_spec()).accepted:
                continue
            result = identify(parsed, mag)
            if not result.confident:
                assert any(a in x for x in result.reasons for a in allowed), (r["title"], result)
