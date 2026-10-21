"""Title-first parsing with a TitleIndex, and agreement between clues."""

from datetime import UTC, datetime

import pytest

from periodicool.decisions import _rank
from periodicool.matching import TitleIndex, TitleSpec, match_title
from periodicool.parser import parse_release
from periodicool.schedule import identify

from dev_magazines import BY_FIXTURE

INDEX = TitleIndex.from_magazines(BY_FIXTURE.values())


def mag(slug):
    return BY_FIXTURE[slug]


@pytest.mark.parametrize(
    "name",
    [
        "2600 - The Hacker Quarterly - 2026-Autumn",
        "2600 - The Hacker Quarterly 2026-Autumn.pdf",
        "2600.The.Hacker.Quarterly.Autumn.2026.pdf",
        "2600 The Hacker Quarterly - Autumn 2026",
    ],
)
def test_2600_keeps_its_number(name):
    parsed = parse_release(name, INDEX)
    assert parsed.issue is None and parsed.season == "autumn" and parsed.year == 2026
    assert match_title(parsed, mag("2600").title_spec()).accepted, parsed


def test_without_index_nothing_changes():
    # The old behavior: a leading "2600 - " is read as a post counter.
    assert (
        parse_release("2600 - The Hacker Quarterly - 2026-Autumn").title == "The Hacker Quarterly"
    )


def test_post_counters_are_still_dropped():
    parsed = parse_release("07 - Fortean Times No 475 October 2026", INDEX)
    assert parsed.title == "Fortean Times" and parsed.issue == 475 and parsed.month == 10


def test_strict_matching_survives():
    parsed = parse_release("People en Espanol - 2026-10-16", INDEX)
    assert parsed.title == "People en Espanol"
    assert not match_title(parsed, mag("people").title_spec()).accepted


def test_longest_title_wins():
    index = TitleIndex([TitleSpec("Linux"), TitleSpec("Linux Pro Magazine", filler_optional=False)])
    assert index.spans("Linux Pro Magazine Issue 311") == [(0, 18)]
    assert index.spans("Linux Format 300") == [(0, 5)]


def test_title_words_are_not_dates_or_regions():
    index = TitleIndex([TitleSpec("Summer Living"), TitleSpec("Australia Today")])
    parsed = parse_release("Summer Living - May 2026", index)
    assert parsed.title == "Summer Living" and parsed.month == 5 and parsed.season is None
    parsed = parse_release("Australia Today - 2026-05", index)
    assert parsed.title == "Australia Today" and parsed.region is None


def test_repeated_title_is_not_read_as_numbers():
    parsed = parse_release("2026-Autumn - 2600 - The Hacker Quarterly", INDEX)
    assert "unrecognized numbers" not in " ".join(parsed.reasons)


def test_filler_words_in_names():
    parsed = parse_release("AMAZONAS Magazine 2026-09", INDEX)
    assert match_title(parsed, mag("amazonas").title_spec()).accepted


# Agreement


def test_number_and_month_agree():
    linux = mag("linux-magazine")
    parsed = parse_release("Linux Magazine USA - Issue 311, October 2026", INDEX)
    ident = identify(parsed, linux)
    assert ident.confident and ident.agreements == ("issue 311 fits 2026-10",)


def test_contradiction_is_still_a_reason():
    linux = mag("linux-magazine")
    ident = identify(parse_release("Linux Magazine USA - Issue 200, October 2026", INDEX), linux)
    assert not ident.confident and not ident.agreements
    assert any("does not fit" in r for r in ident.reasons)


def test_single_clue_has_no_agreement():
    ident = identify(parse_release("Linux Magazine - 2026-10", INDEX), mag("linux-magazine"))
    assert ident.confident and ident.agreements == ()


def test_thirteen_per_year_agreement():
    ft = mag("fortean-times")
    ident = identify(parse_release("Fortean Times No 437 November 2023", INDEX), ft)
    assert ident.agreements == ("issue 437 fits 2023-11",)


def test_weekly_publishing_day():
    people = mag("people")
    ident = identify(parse_release("People USA - 2026-10-19", INDEX), people)  # a Monday
    assert ident.agreements == ("Monday is the publishing day",)


def test_agreement_breaks_ties_in_rank():
    linux = mag("linux-magazine")
    parsed = parse_release("Linux Magazine - 2026-10.pdf", INDEX)
    when = datetime(2026, 10, 1, tzinfo=UTC)
    assert _rank(parsed, linux, 10, when, agreements=1) < _rank(parsed, linux, 10, when)
