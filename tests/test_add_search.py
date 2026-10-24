"""The Add magazine search: guesses from real release names, merging with catalog
records, and the search, pick and add flow over HTTP."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import add_search
from periodicool.lookups import Record
from periodicool.models import Frequency, LookupCache, Magazine, Numbering
from test_lookups import ISSN, T2600, fake, turn_on  # noqa: F401

from fake_prowlarr import release_json
from test_web_magazines import fakes, mags, session, toast  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "releases"


def top_card(fixture: str) -> add_search.Card:
    data = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    return add_search.from_releases([r["title"] for r in data["releases"]])[0]


# Guesses checked against the real names (see PROGRESS for where they differ from
# what CLAUDE.md expected).
@pytest.mark.parametrize(
    ("fixture", "title_key", "numbering", "frequency"),
    [
        ("2600.json", "2600 hacker quarterly", Numbering.SEASON, Frequency.SEASONAL),
        ("amazonas.json", "amazonas", Numbering.DATE, Frequency.BIMONTHLY),
        (
            "american-cinematographer.json",
            "american cinematographer",
            Numbering.DATE,
            Frequency.MONTHLY,
        ),
        # Fortean Times releases are mostly date-named; the numbers still say 13 a year.
        ("fortean-times.json", "fortean times", Numbering.DATE, Frequency.THIRTEEN_PER_YEAR),
        ("linux-magazine.json", "linux", Numbering.ISSUE, Frequency.MONTHLY),
        ("people.json", "people", Numbering.DATE, Frequency.WEEKLY),
        ("tabletop-gaming.json", "tabletop gaming", Numbering.ISSUE, Frequency.MONTHLY),
    ],
)
def test_guesses_from_real_release_names(fixture, title_key, numbering, frequency):
    card = top_card(fixture)
    assert card.key == title_key
    assert (card.guess.numbering, card.guess.frequency) == (numbering, frequency)
    assert card.guess.anchor_date is not None and len(card.examples) == 3


def test_make_is_volume_numbered_and_linux_has_its_latest_number():
    assert top_card("make.json").guess.numbering is Numbering.VOLUME
    assert top_card("linux-magazine.json").guess.anchor_number == 312


def test_noise_is_left_out():
    cards = add_search.from_releases(["Some One Off - March 2020", "People - 12 October 2023"])
    assert cards == []  # one name each is not enough to call it a magazine


def test_merge_records_with_releases_and_each_other():
    cards = add_search.from_releases(
        [
            "2600 Magazine The Hacker Quarterly - Spring 2016",
            "2600 The Hacker Quarterly - Summer 2016",
        ]
    )
    loc = Record(
        source="loc",
        title="2600 : the hacker quarterly",
        issns=[ISSN],
        publisher="2600 Enterprises",
    )
    wiki = Record(
        source="wikidata",
        title="2600: The Hacker Quarterly",
        issns=[ISSN],
        alt_titles=["2600 Magazine"],
    )
    other = Record(source="wikidata", title="2600 Club", issns=["1234-5679"])
    merged = add_search.merge(T2600, [loc, wiki, other], cards, tracked={"2600 club": 9})
    first = merged[0]
    assert first.releases == 2 and first.sources == ["loc", "wikidata"]
    assert first.issns == [ISSN] and "2600 Magazine" in first.alt_titles
    club = next(c for c in merged if c.key == "2600 club")
    assert club.tracked_id == 9 and club.releases == 0


# Over HTTP


def test_search_pick_and_add(client, fakes, fake):  # noqa: F811
    fake_p, _ = fakes
    fake_p.search_results = [
        release_json(t, guid=f"g{i}")
        for i, t in enumerate(
            [
                "2600 Magazine The Hacker Quarterly - Spring 2016",
                "2600 Magazine The Hacker Quarterly - Summer 2016",
                "2600 Magazine The Hacker Quarterly - Autumn 2016",
            ]
        )
    ]
    turn_on(client, "wikidata")
    page = client.get("/magazines/add").text
    assert "Searches your indexers and the catalogs you turned on." in page
    results = client.post("/magazines/add/search", data={"term": T2600}).text
    assert "3 releases" in results and "Wikidata" in results and ISSN in results
    searches = [c for c in fake_p.calls if c["path"] == "/api/v1/search"]
    client.post("/magazines/add/search", data={"term": T2600})
    # Every search asks the indexers live; nothing is served from an earlier search.
    assert len([c for c in fake_p.calls if c["path"] == "/api/v1/search"]) > len(searches)
    form = client.post(
        "/magazines/add/pick", data={"term": T2600, "key": "2600 hacker quarterly"}
    ).text
    assert 'value="seasonal" selected' in form and 'value="season" selected' in form
    assert f'value="{ISSN}"' in form and "More options" in form
    calls = len(fake.calls)
    resp = client.post(
        "/magazines/add",
        data={
            "title": "2600: The Hacker Quarterly",
            "frequency": "seasonal",
            "numbering": "season",
            "monitor_mode": "new_only",
            "monitored": "on",
            "issn": ISSN,
            "languages": "english",
            "grace_days": "14",
            "pick_term": T2600,
            "pick_key": "2600 hacker quarterly",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 204 and resp.headers["HX-Redirect"].startswith("/magazines/")
    with session(client) as s:
        mag = s.scalars(select(Magazine)).one()
        assert mag.issn == ISSN and mag.frequency is Frequency.SEASONAL
        assert s.scalars(select(LookupCache).where(LookupCache.query == f"issn:{ISSN}")).first()
        mag_id = mag.id
    popup = client.get(f"/magazines/{mag_id}/metadata").text
    assert ISSN in popup
    assert len(fake.calls) == calls  # the Metadata pop-up answered from what was kept
    tracked = client.post("/magazines/add/search", data={"term": T2600}).text
    assert "Already tracked" in tracked


def test_add_errors_stay_in_the_pop_up(client):
    resp = client.post(
        "/magazines/add",
        data={"title": "", "frequency": "monthly", "numbering": "date"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200 and "Enter a title" in resp.text and "<html" not in resp.text
    manual = client.get("/magazines/add/manual", params={"term": "Wired"}).text
    assert 'value="Wired"' in manual and "More options" in manual


def test_catalog_title_search_keeps_magazines_only():
    from periodicool.lookups import loc

    def record(leader, title):
        return (
            f'<record xmlns="http://www.loc.gov/MARC21/slim"><leader>{leader}</leader>'
            f'<datafield tag="245"><subfield code="a">{title}</subfield></datafield></record>'
        )

    body = (
        "<searchRetrieveResponse><records>"
        + record("00000cam a2200000 a 4500", "A book about Fortean things")
        + record("00000cas a2200000 a 4500", "Fortean times")
        + "</records></searchRetrieveResponse>"
    ).encode()
    found = loc.candidates(
        title="Fortean Times", transport=lambda url, headers, timeout: (200, body)
    )
    assert [r.title for r in found] == ["Fortean times"]
