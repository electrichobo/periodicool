"""Title matching against the dev magazine specs, using real fixture names."""

import pytest

from periodicool.matching import TitleSpec, match_title, normalize_title
from periodicool.parser import parse_release

from dev_magazines import (
    AMAZONAS,
    AMERICAN_CINEMATOGRAPHER,
    FORTEAN_TIMES,
    HACKER_QUARTERLY,
    LINUX_MAGAZINE,
    MAKE,
    PEOPLE,
    TABLETOP_GAMING,
)

ACCEPT = [
    (AMERICAN_CINEMATOGRAPHER, "American.Cinematographer.Magazine-April.2013"),
    (AMAZONAS, "Amazonas-July.August.2013"),
    (HACKER_QUARTERLY, "2600.Magazine.The.Hacker.Quarterly-Autumn.2013"),
    (LINUX_MAGAZINE, "Linux.Magazine.No.261.2022.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE"),
    (LINUX_MAGAZINE, "Linux Magazine USA - Issue 310, September 2026"),
    (LINUX_MAGAZINE, "Linux_Magazine_-_March_2026"),
    (LINUX_MAGAZINE, "Linux.Magazine.Special.Editions-17.October.2022"),
    (MAKE, "MAKE Magazine vol 05"),
    (MAKE, "Make.No.98.2026.RETAIL.MAGAZINE.eBook-21A1"),
    (MAKE, "Make magazine- Volume 33, 2013"),
    (FORTEAN_TIMES, "15-Fortean-Times-February-2013.pdf-yEnc"),
    (FORTEAN_TIMES, "Fortean.Times.UK-May.2014"),
    (FORTEAN_TIMES, "Fortean.Times.Paranormal.Handbook.TruePDF.2013"),
    (TABLETOP_GAMING, "Tabletop.Gaming.No.119.2026.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE"),
    (PEOPLE, "People.USA.TruePDF-22.August.2016"),
    (PEOPLE, "People - Vol. 91 No. 22 27 May 2019 1 of 9 People - Vol. 91 No. 22 27 May 2019.jpg"),
    (PEOPLE, "People.Special.Edition-07.July.2023"),
    (PEOPLE, "People.Bookazines.TruePDF-March.2019"),
]

REJECT = [
    # (spec, name, reason fragment)
    (
        HACKER_QUARTERLY,
        "15 - MAGAZINE DeSoto.Magazine-November.2016.pdf.par2 - 2600 MB "
        "usenet-space-cowboys.online",
        "title",
    ),
    (LINUX_MAGAZINE, "15 - MAGAZINE  Linux", "title"),
    (LINUX_MAGAZINE, "Linux-Magazin.No.06.2026.GERMAN.HYBRID.MAGAZINE.eBook-LORENZ", "title"),
    (LINUX_MAGAZINE, "Linux.Pro.No.235.2026.iTALiAN.HYBRiD.MAGAZiNE", "title"),
    (LINUX_MAGAZINE, "Linux.Format.No.293.2022.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE", "title"),
    (LINUX_MAGAZINE, "[MAGAZINE] Linux.Voice.TruePDF-December.2015", "title"),
    (MAKE, "Make.Your.Own.PC.Tricks.And.Tips.No.23.2026.HYBRID.MAGAZINE", "title"),
    (MAKE, "WineMaker.April-May.2023.HYBRID.MAGAZINE.eBook-21A1", "title"),
    (MAKE, "Passagemaker.October.2026.RETAIL.MAGAZINE", "title"),
    (
        MAKE,
        "15 - MAGAZINE delicious.UK.Make.It.Everyday.TruePDF-Issue.5.2016.pdf.par2 - "
        "2998 MB usenet-space-cowboys.online",
        "title",
    ),
    (MAKE, "Woodworking - MAKE.Magazine.Vol.30.PDF.English.rar - 167178", "title"),
    (PEOPLE, "People.Australia.TruePDF-08.April.2019", "region Australia"),
    (PEOPLE, "People.South.Africa.TruePDF-26.August.2016", "region South Africa"),
    (PEOPLE, "People.en.Espaol.TruePDF-June.2019", "title"),
    (PEOPLE, "Real.People.TruePDF-02.May.2019", "title"),
    (PEOPLE, "Sunday.People.[2023.07.09]", "title"),
    (PEOPLE, "The.People.2010.01.10.Retail.Ebook-PDFWriters", "title"),
    (PEOPLE, "The.People.s.Friend.Special.Issue.249-30.September.2023", "title"),
    (PEOPLE, "[MAGAZINE] People.Matters.TruePDF-August.2016", "title"),
    (PEOPLE, "People.StyleWatch.TruePDF-December.2014", "title"),
    (PEOPLE, "PEOPLE.Barbie-June.2023", "title"),
    (PEOPLE, "People.USA.Wellness.Special.TruePDF-2016", "title"),
    (PEOPLE, "TIME.Magazine.USA.Special.Edition.People.We.Lost.2025.2025.HYBRID.MAGAZINE", "title"),
]


@pytest.mark.parametrize(("spec", "name"), ACCEPT)
def test_accept(spec, name):
    result = match_title(parse_release(name), spec.title_spec())
    assert result.accepted, result.reason


@pytest.mark.parametrize(("spec", "name", "reason"), REJECT)
def test_reject(spec, name, reason):
    result = match_title(parse_release(name), spec.title_spec())
    assert not result.accepted
    assert reason in result.reason


def test_normalize_title():
    assert normalize_title("2600: The Hacker Quarterly") == "2600 the hacker quarterly"
    assert normalize_title("Make:") == "make"
    assert normalize_title("People en Español") == "people en espanol"
    assert normalize_title("Linux Magazine", drop_filler=False) == "linux magazine"


def test_language_filter():
    spec = TitleSpec("Linux Pro")
    result = match_title(parse_release("Linux.Pro.No.235.2026.iTALiAN.HYBRiD.MAGAZiNE"), spec)
    assert not result.accepted and "italian" in result.reason
    italian = TitleSpec("Linux Pro", languages=("italian",))
    assert match_title(
        parse_release("Linux.Pro.No.235.2026.iTALiAN.HYBRiD.MAGAZiNE"), italian
    ).accepted


def test_aliases():
    spec = TitleSpec("Linux Magazine", aliases=("Linux Pro Magazine",), filler_optional=False)
    assert match_title(parse_release("Linux.Pro.Magazine-May.2015"), spec).accepted
