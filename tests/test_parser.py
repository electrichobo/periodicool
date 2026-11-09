"""Hand-checked parses of real release names from tests/fixtures/releases."""

import json
from pathlib import Path

import pytest

from periodicool.parser import HIGH, LOW, clean_name, parse_release

ALL_NAMES = {
    r["title"]
    for path in (Path(__file__).parent / "fixtures" / "releases").glob("*.json")
    for r in json.loads(path.read_text(encoding="utf-8"))["releases"]
}

# (name, expected fields). Fields not listed must be None/False/empty.
CASES = [
    # Month and year, with and without "Magazine", TruePDF and post junk.
    (
        "American.Cinematographer-April.2023",
        {"title": "American Cinematographer", "year": 2023, "month": 4},
    ),
    (
        "American.Cinematographer.TruePDF-September.2007",
        {
            "title": "American Cinematographer",
            "year": 2007,
            "month": 9,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "14 - MAGAZINE American.Cinematographer-October.2016.pdf.par2 - 1958 MB "
        "usenet-space-cowboys.online",
        {"title": "American Cinematographer", "year": 2016, "month": 10, "formats": ("pdf",)},
    ),
    (
        "American.Cinematographer.Magazine.November.2013.- SPIDAROT.SHARE -",
        {"title": "American Cinematographer Magazine", "year": 2013, "month": 11},
    ),
    (
        "American.Cinematographer.??.TruePDF-September.2016",
        {
            "title": "American Cinematographer",
            "year": 2016,
            "month": 9,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    # ISO year-month.
    (
        "American.Cinematographer.2024-02",
        {"title": "American Cinematographer", "year": 2024, "month": 2},
    ),
    # Bimonthly month pairs.
    (
        "Amazonas.Magazine-November.December.2013",
        {"title": "Amazonas Magazine", "year": 2013, "month": 11, "month_end": 12},
    ),
    (
        "Make.Magazine.TruePDF-April.May.2015",
        {
            "title": "Make Magazine",
            "year": 2015,
            "month": 4,
            "month_end": 5,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    # Seasons.
    (
        "2600.Magazine.The.Hacker.Quarterly-Winter.2015",
        {"title": "2600 Magazine The Hacker Quarterly", "year": 2015, "season": "winter"},
    ),
    (
        "Tabletop.Gaming.Issue.2.TruePDF-Autumn.2015",
        {
            "title": "Tabletop Gaming",
            "year": 2015,
            "season": "autumn",
            "issue": 2,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "MovieMaker.Magazine.TruePDF-Fall.2019",
        {
            "title": "MovieMaker Magazine",
            "year": 2019,
            "season": "autumn",
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    # Issue numbers in several spellings.
    (
        "Fortean.Times.Issue.351.TruePDF-March.2017",
        {
            "title": "Fortean Times",
            "year": 2017,
            "month": 3,
            "issue": 351,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "Fortean.Times.No.416.2022.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE",
        {
            "title": "Fortean Times",
            "year": 2022,
            "issue": 416,
            "quality": ("hybrid",),
            "group": "LiBRiCiDE",
        },
    ),
    (
        "Linux Magazine USA - Issue 310, September 2026",
        {"title": "Linux Magazine", "region": "USA", "year": 2026, "month": 9, "issue": 310},
    ),
    (
        "Linux.Magazine.USA.Issue.261-August.2022",
        {"title": "Linux Magazine", "region": "USA", "year": 2022, "month": 8, "issue": 261},
    ),
    (
        "Linux Magazine #03 (2024)",
        {"title": "Linux Magazine", "year": 2024, "issue": 3},
    ),
    (
        "Tabletop.Gaming.Issue.87.2024-02",
        {"title": "Tabletop Gaming", "year": 2024, "month": 2, "issue": 87},
    ),
    (
        "Fortean Times - Issue 380 Jun 2019 1 of 7 Fortean Times - Issue 380 Jun 2019.jpg",
        {"title": "Fortean Times", "year": 2019, "month": 6, "issue": 380},
    ),
    (
        "15-Fortean-Times-February-2013.pdf-yEnc",
        {"title": "Fortean-Times", "year": 2013, "month": 2, "formats": ("pdf",)},
    ),
    (
        "Fortean.Times.UK-May.2014",
        {"title": "Fortean Times", "region": "UK", "year": 2014, "month": 5},
    ),
    # Volume numbers.
    ("MAKE Magazine vol 05", {"title": "MAKE Magazine", "volume": 5}),
    (
        "[[ MAKE Magazine vol 05 ]] - MAKE Magazine vol 05 - (0005/1027)",
        {"title": "MAKE Magazine", "volume": 5},
    ),
    (
        "MAKE Magazine Vol.01 True PDF English - MAKE Magazine vol 01.par2 - 16",
        {
            "title": "MAKE Magazine",
            "volume": 1,
            "language": "english",
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    ("MAKE.Magazine-Volume35.2013", {"title": "MAKE Magazine", "year": 2013, "volume": 35}),
    (
        "Make.Magazine.Vol.41.September.2014.RETAiL.eBOOk-NEWSPAPER",
        {
            "title": "Make Magazine",
            "year": 2014,
            "month": 9,
            "volume": 41,
            "quality": ("retail",),
            "group": "NEWSPAPER",
        },
    ),
    ("Make magazine- Volume 33, 2013", {"title": "Make magazine", "year": 2013, "volume": 33}),
    # Weekly: day, month, year; volume and issue together; quoted post subjects.
    (
        "People.USA.TruePDF-1.December.2017",
        {
            "title": "People",
            "region": "USA",
            "year": 2017,
            "month": 12,
            "day": 1,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "[Gossip Magazine package for 07 Jun 2021 - Supplemental] [2 of 9] "
        '"People - Vol. 95 No. 23 [07 Jun 2021] (TruePDF).pdf" yEnc',
        {
            "title": "People",
            "year": 2021,
            "month": 6,
            "day": 7,
            "volume": 95,
            "issue": 23,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "[People South Africa - Vol. 33 No. 51 & 52 [27 Dec 2019]] [2 of 8] "
        '"People South Africa - Vol. 33 No. 51 & 52 [27 Dec 2019].pdf" yEnc',
        {
            "title": "People",
            "region": "South Africa",
            "year": 2019,
            "month": 12,
            "day": 27,
            "volume": 33,
            "issue": 51,
            "issue_end": 52,
            "formats": ("pdf",),
        },
    ),
    (
        "Sunday.People.[2023.07.09]",
        {"title": "Sunday People", "year": 2023, "month": 7, "day": 9},
    ),
    (
        "Tabletop.Gaming-25.November.2022",
        {"title": "Tabletop Gaming", "year": 2022, "month": 11, "day": 25},
    ),
    # Editions and languages.
    (
        "People.Magazine.South.Africa.TruePDF-16.August.2013",
        {
            "title": "People Magazine",
            "region": "South Africa",
            "year": 2013,
            "month": 8,
            "day": 16,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "People.en.Espaol.TruePDF-June.2019",
        {
            "title": "People en Espaol",
            "year": 2019,
            "month": 6,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
    (
        "Linux-Magazin.No.06.2026.GERMAN.HYBRID.MAGAZINE.eBook-LORENZ",
        {
            "title": "Linux-Magazin",
            "language": "german",
            "year": 2026,
            "issue": 6,
            "quality": ("hybrid",),
            "group": "LORENZ",
        },
    ),
    (
        "Linux.Magazin.2017.12.GERMAN.RETAiL.MAGAZiNE.eBOOk-sUppLeX",
        {
            "title": "Linux Magazin",
            "language": "german",
            "year": 2017,
            "month": 12,
            "quality": ("retail",),
            "group": "sUppLeX",
        },
    ),
    (
        "Linux.User.11.2015.German.Retail.Magazine.PROPER.READ",
        {
            "title": "Linux User",
            "language": "german",
            "year": 2015,
            "month": 11,
            "quality": ("retail", "proper"),
        },
    ),
    # Specials.
    (
        "People.Special.Edition-07.July.2023",
        {"title": "People", "special": True, "year": 2023, "month": 7, "day": 7},
    ),
    (
        "Linux.Magazine.Special.Editions-17.October.2022",
        {"title": "Linux Magazine", "special": True, "year": 2022, "month": 10, "day": 17},
    ),
    (
        "People.Bookazines.TruePDF-March.2019",
        {
            "title": "People",
            "special": True,
            "year": 2019,
            "month": 3,
            "true_pdf": True,
            "formats": ("pdf",),
        },
    ),
]

LOW_CASES = [
    # (name, reason fragment)
    ("American.Cinematographer.TruePDF-2016", "no issue identity"),
    ("Fortean.Times.TruePDF-October", "month without year"),
    ("People.USA.TruePDF-12", "unrecognized numbers 12"),
    ("People USA 02 10 2025", "ambiguous numeric date"),
    ("Tabletop.Gaming.08.09.TruePDF-2017", "unrecognized numbers 08 09"),
    ("Real People - Issue 27 11 Jul 2109 1 of 7 Real People - Issue 27 11 Jul 2109.jpg", "month"),
    ("MAKE.Magazine.01-45.Mega.Pack.-.DIY.Projects.and.Ideas.for.Makers.TruePDF", "pack"),
    ("0137 - MAGAZINE MAKE Magazine 01-45.par2 - 314 GB usenet-space-cowboys.online", "pack"),
    (
        "MAKE.Magazine.Complete.eBook.Collection.Volumes.01-37.2005-2014.True.PDF.English",
        "pack",
    ),
    ("People.Special.Edition.3F.Yearbook.2023.[2024]", "conflicting years"),
    ("Fortean.Times.Paranormal.Handbook.TruePDF.2013", "no issue identity"),
    ("15 - MAGAZINE  Linux", "no issue identity"),
    ("MAKE_Magazine_-_Vol", "no issue identity"),
]

DEFAULTS = {
    "region": None,
    "language": None,
    "year": None,
    "month": None,
    "month_end": None,
    "day": None,
    "season": None,
    "week": None,
    "issue": None,
    "issue_end": None,
    "volume": None,
    "volume_end": None,
    "special": False,
    "pack": False,
    "true_pdf": False,
    "formats": (),
    "quality": (),
    "group": None,
}


def test_cases_are_real_fixture_names():
    names = [n for n, _ in CASES] + [n for n, _ in LOW_CASES]
    missing = [n for n in names if n not in ALL_NAMES]
    assert not missing, f"not in fixtures: {missing}"


@pytest.mark.parametrize(("name", "expected"), CASES)
def test_parse(name, expected):
    parsed = parse_release(name)
    want = DEFAULTS | expected
    got = {k: getattr(parsed, k) for k in ["title", *DEFAULTS]}
    assert got == want
    assert parsed.confidence == HIGH, parsed.reasons


@pytest.mark.parametrize(("name", "reason"), LOW_CASES)
def test_low_confidence(name, reason):
    parsed = parse_release(name)
    assert parsed.confidence == LOW
    assert any(reason in r for r in parsed.reasons), parsed.reasons


def test_html_entities_decoded():
    text, _ = clean_name(
        "Selfbuilder.&amp;.homemaker-full.year.2016.issues.collection.magazine.true.pdf-ng16-0102"
    )
    assert "&amp;" not in text and "&" in text


def test_packs_and_double_issues():
    pack = parse_release(
        "MAKE.Magazine.[01-45].Mega.Pack-DIY.Projects.and.Ideas.for.Makers.[TruePDF]"
    )
    assert (pack.pack, pack.issue, pack.issue_end) == (True, 1, 45)
    double = parse_release(
        "[People South Africa - Vol. 33 No. 51 & 52 [27 Dec 2019]] [2 of 8] "
        '"People South Africa - Vol. 33 No. 51 & 52 [27 Dec 2019].pdf" yEnc'
    )
    assert not double.pack


def test_week_numbers():
    # No fixture uses week numbers yet; covered so the pattern is not dead code.
    parsed = parse_release("Some.Weekly.Week.12.2024")
    assert (parsed.title, parsed.year, parsed.week) == ("Some Weekly", 2024, 12)
    assert parsed.confidence == HIGH


@pytest.mark.parametrize("name", sorted(ALL_NAMES))
def test_every_fixture_parses_without_error(name):
    parsed = parse_release(name)
    if parsed.month is not None:
        assert 1 <= parsed.month <= 12
    if parsed.day is not None:
        assert 1 <= parsed.day <= 31
    if parsed.year is not None:
        assert 1900 <= parsed.year <= 2099


# Real names from fixtures/releases/new-york-magazine.json. The issue date is the
# first day of the range, whichever order or tags the name uses.
@pytest.mark.parametrize(
    ("name", "year", "month", "day"),
    [
        # Month first at both ends.
        ("New.York.Magazine.No.October.5th-October.18th.2026.HYBRiD.MAGAZiNE", 2026, 10, 5),
        ("New.York.Magazine.No.October.19th-November.1st.2026.HYBRiD.MAGAZiNE", 2026, 10, 19),
        ("New.York.Magazine.No.April.6th-April.19th.2026.HYBRiD.MAGAZiNE", 2026, 4, 6),
        # One month, two days, in both orders.
        ("New.York.Magazine.No.January.15-28.2024.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE", 2024, 1, 15),
        ("New.York.Magazine-14-27.August.2023", 2023, 8, 14),
        ("New.York.Magazine.TruePDF-10-23.July.2017", 2017, 7, 10),
        # A year at each end.
        ("New.York.Magazine-30.December.2024-12.January.2025.[NulledPremium]", 2024, 12, 30),
        # Mixed orders, and a tag in the middle of the range.
        ("New.York.Magazine.October.23-5.November.2023", 2023, 10, 23),
        ("New.York.Magazine.30.October.TruePDF-12.November.2017", 2017, 10, 30),
        ("New.York.Magazine.August.21.TruePDF-3.September.2017", 2017, 8, 21),
        ("New.York.magazine.6.TruePDF-19.October.2014", 2014, 10, 6),
        # Only the end has a year, and the range crosses New Year.
        ("Foo December 29th-January 11th 2026", 2025, 12, 29),
        ("Foo December 29th 2025-January 11th 2026", 2025, 12, 29),
        # Commas and en dashes.
        ("Foo October 5–18, 2026", 2026, 10, 5),
        ("Foo Oct. 5-18, 2026", 2026, 10, 5),
    ],
)
def test_date_range_uses_the_first_day(name, year, month, day):
    parsed = parse_release(name)
    assert (parsed.year, parsed.month, parsed.day) == (year, month, day)
    assert parsed.confidence == HIGH, parsed.reasons
    assert parsed.issue is None and not parsed.pack


@pytest.mark.parametrize(
    "name",
    [
        "Foo 1 January 2020-31 December 2025",  # far too long for one issue
        "Foo 5 October 2026-1 October 2026",  # ends before it starts
        "Foo February 20-31 2026",  # no such day
    ],
)
def test_date_range_that_makes_no_sense_is_not_guessed(name):
    parsed = parse_release(name)
    assert parsed.confidence == LOW
    assert parsed.day is None


@pytest.mark.parametrize(
    ("name", "month", "day"),
    [
        ("The New York Times Magazine - August 30, 2026", 8, 30),
        ("People October 6 2025", 10, 6),
        ("People October 6th 2025", 10, 6),
        ("People Oct-06-2025", 10, 6),
    ],
)
def test_month_first_date(name, month, day):
    parsed = parse_release(name)
    assert (parsed.month, parsed.day) == (month, day)
    assert parsed.year in (2025, 2026)
    assert parsed.confidence == HIGH, parsed.reasons
    assert "October" not in parsed.title and "Oct" not in parsed.title


@pytest.mark.parametrize(
    ("name", "issue", "day", "month", "year"),
    [
        # The dash after an issue number separates it from the date: not a range.
        ("The.Peoples.Friend.Issue.7991-19.August.2023", 7991, 19, 8, 2023),
        ("Foo No 10-12 January 2026", 10, 12, 1, 2026),
        ("Foo Issue 10-12 January 2026", 10, 12, 1, 2026),
    ],
)
def test_issue_number_then_date_is_not_a_range(name, issue, day, month, year):
    parsed = parse_release(name)
    assert (parsed.issue, parsed.day, parsed.month, parsed.year) == (issue, day, month, year)
    assert parsed.issue_end is None


def test_issue_number_and_day_both_read():
    parsed = parse_release("New York Magazine - Vol. 52 No. 10 13 May 2019")
    assert (parsed.volume, parsed.issue) == (52, 10)
    assert (parsed.year, parsed.month, parsed.day) == (2019, 5, 13)


@pytest.mark.parametrize("name", ["Foo 31 February 2026", "Foo February 31 2026"])
def test_impossible_date_is_low_confidence(name):
    parsed = parse_release(name)
    assert parsed.confidence == LOW
    assert any("impossible date" in r for r in parsed.reasons), parsed.reasons
    assert parsed.day is None
