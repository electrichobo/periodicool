"""The indexer registry: kinds, Sonarr's presets, API path and additional parameters."""

from fake_newznab import NZ_KEY, NZ_URL, FakeNewznab
from periodicool import indexer_kinds
from periodicool.newznab import NewznabClient


def test_kinds_and_protocols():
    assert indexer_kinds.INDEXER_KINDS == {
        "prowlarr": "Prowlarr",
        "newznab": "Newznab",
        "torznab": "Torznab",
    }
    assert indexer_kinds.INDEXER_PROTOCOLS["torznab"] == "torrent"
    for kind in indexer_kinds.KINDS.values():
        names = [f.name for f in kind.fields]
        assert names[0] == "url" and "api_key" in names and "categories" in names


def test_advanced_fields_are_marked():
    newznab = {f.name: f for f in indexer_kinds.KINDS["newznab"].fields}
    assert newznab["api_path"].advanced and newznab["additional_parameters"].advanced
    assert not newznab["url"].advanced and not newznab["categories"].advanced
    torznab = {f.name: f for f in indexer_kinds.KINDS["torznab"].fields}
    assert torznab["reject_blocklisted_hashes"].advanced and not torznab["seed_ratio"].advanced


def test_sonarr_presets():
    names = [p.name for p in indexer_kinds.KINDS["newznab"].presets]
    assert names[:2] == ["DOGnzb", "DrunkenSlug"] and len(names) == 9
    tabula = indexer_kinds.preset("newznab", "Tabula Rasa")
    assert tabula.url == "https://www.tabula-rasa.pw" and tabula.api_path == "/api/v1/api"
    assert indexer_kinds.preset("newznab", "NZBgeek").api_path == "/api"
    assert indexer_kinds.preset("torznab", "NZBgeek") is None
    assert indexer_kinds.defaults("newznab")["api_path"] == "/api"


def test_additional_parameters():
    parse = indexer_kinds.parse_parameters
    assert parse("&maxage=1000&foo=bar") == ([("maxage", "1000"), ("foo", "bar")], None)
    assert parse("") == ([], None)
    assert "set by PeriodiCool" in parse("&apikey=steal")[1]
    assert "set by PeriodiCool" in parse("t=caps")[1]
    assert parse("justtext")[1]


def test_api_path_and_parameters_reach_the_indexer():
    fake = FakeNewznab(api_path="/api/v1/api")
    nz = NewznabClient(
        NZ_URL + "/api/v1/api",  # pasted with the path; trimmed once
        NZ_KEY,
        api_path="/api/v1/api",
        extra_params=[("maxage", "1000")],
        transport=fake,
        min_interval=0,
    )
    nz.search("Linux Magazine", [7010])
    call = fake.calls[-1]
    assert call["path"] == "/api/v1/api" and call["query"]["maxage"] == "1000"
    nz.caps()
    assert "maxage" not in fake.calls[-1]["query"]  # caps is asked as is
