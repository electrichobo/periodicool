from datetime import date
from pathlib import PurePosixPath

import pytest

from periodicool.models import Frequency, Magazine, Numbering
from periodicool.naming import DEFAULT_TEMPLATE, UnsafePathError, inside, render, sanitize

from dev_magazines import (
    FORTEAN_TIMES,
    HACKER_QUARTERLY,
    LINUX_MAGAZINE,
    MAKE,
    PEOPLE,
    TABLETOP_GAMING,
)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("2600: The Hacker Quarterly", "2600 - The Hacker Quarterly"),
        ("Make:", "Make"),
        ('Bad*Name?"<>|', "BadName"),
        ("AC/DC\\Live", "AC-DC-Live"),
        ("Trailing dots...", "Trailing dots"),
        ("  spaced   out  ", "spaced out"),
        ("People en Español", "People en Español"),
        ("tab\there\x00", "tabhere"),
        ("..", ""),
    ],
)
def test_sanitize(raw, clean):
    assert sanitize(raw) == clean


def _r(mag, fields, ext="pdf", key=None):
    return render(DEFAULT_TEMPLATE, mag, fields, ext, key).as_posix()


def test_monthly():
    assert _r(LINUX_MAGAZINE, {"year": 2023, "month": 11, "issue_number": 276}) == (
        "Linux Magazine/2023/Linux Magazine - 2023-11.pdf"
    )


def test_month_from_cover_date():
    fields = {"issue_number": 261, "cover_date": date(2022, 8, 1)}
    assert _r(LINUX_MAGAZINE, fields) == "Linux Magazine/2022/Linux Magazine - 2022-08.pdf"


def test_weekly_appends_day():
    assert (
        _r(PEOPLE, {"year": 2017, "month": 12, "day": 4}) == "People/2017/People - 2017-12-04.pdf"
    )


def test_monthly_ignores_day():
    fields = {"year": 2022, "month": 11, "day": 25, "issue_number": 72}
    assert _r(TABLETOP_GAMING, fields, "cbz") == (
        "Tabletop Gaming Magazine/2022/Tabletop Gaming Magazine - 2022-11.cbz"
    )


def test_seasonal_and_colon_title():
    fields = {"year": 2015, "season": "winter", "cover_date": "2015-12-01"}
    assert _r(HACKER_QUARTERLY, fields) == (
        "2600 - The Hacker Quarterly/2015/2600 - The Hacker Quarterly - 2015-Winter.pdf"
    )


def test_no_date_drops_year_folder():
    assert _r(MAKE, {"volume_number": 5}, "PDF") == "Make/Make - Vol 05.pdf"
    numbered = Magazine(title="Zine", frequency=Frequency.IRREGULAR, numbering=Numbering.ISSUE)
    assert _r(numbered, {"issue_number": 12}) == "Zine/Zine - No 12.pdf"


def test_special():
    fields = {"year": 2023, "month": 7, "day": 7, "special": True}
    assert _r(PEOPLE, fields) == "People/2023/People - 2023-07-07 - Special.pdf"


def test_second_issue_in_a_month_keeps_suffix():
    fields = {"year": 2022, "month": 8, "issue_number": 421}
    assert _r(FORTEAN_TIMES, fields, key="2022-08-2") == (
        "Fortean Times/2022/Fortean Times - 2022-08-2.pdf"
    )


def test_custom_template_tokens():
    template = "{Title}/{Title} {Year} No {Issue}.{ext}"
    path = render(template, LINUX_MAGAZINE, {"year": 2023, "month": 11, "issue_number": 276}, "pdf")
    assert path == PurePosixPath("Linux Magazine/Linux Magazine 2023 No 276.pdf")


def test_traversal_is_neutralized():
    evil = Magazine(title="../../etc", frequency=Frequency.MONTHLY, numbering=Numbering.DATE)
    path = _r(evil, {"year": 2020, "month": 1})
    assert ".." not in PurePosixPath(path).parts
    sneaky = render("{Title}/../../{Title}.{ext}", evil, {}, "pdf")
    assert ".." not in sneaky.parts
    with pytest.raises(UnsafePathError):
        render("{Year}/{Month}", evil, {}, "pdf")  # renders empty


def test_inside_guard(tmp_path):
    assert inside(tmp_path, PurePosixPath("a/b.pdf")) == (tmp_path / "a" / "b.pdf").resolve()
    with pytest.raises(UnsafePathError):
        inside(tmp_path, PurePosixPath("../escape.pdf"))
