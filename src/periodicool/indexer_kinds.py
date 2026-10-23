"""Every indexer kind PeriodiCool can search, with the fields its Settings pop-up
asks for (Sonarr's, per kind) and Sonarr's Newznab presets. The registry drives the
Add picker, the form and building a working indexer client.

Presets only fill in the form; nothing is contacted until the user tests or saves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from periodicool.clients.registry import Field

DEFAULT_API_PATH = "/api"
MAGAZINES = 7010  # Newznab's Books > Magazines
BOOK_CATEGORIES = (
    (7000, "Books"),
    (7010, "Books > Magazines"),
    (7020, "Books > Ebook"),
    (7030, "Books > Comics"),
)
# Query keys PeriodiCool sets itself; additional parameters may not override them.
RESERVED_PARAMETERS = {"t", "q", "cat", "apikey", "extended", "limit", "offset", "o"}


@dataclass(frozen=True)
class Preset:
    name: str
    url: str
    api_path: str = DEFAULT_API_PATH


@dataclass(frozen=True)
class IndexerKind:
    kind: str
    label: str
    protocol: str  # usenet or torrent
    info: str
    fields: tuple[Field, ...]
    build: Callable  # (row, key, settings, common) -> client
    transport_key: str
    presets: tuple[Preset, ...] = ()


def _url(label: str, help: str, placeholder: str) -> Field:
    return Field(
        "url", label, "url", help=help, required=True, store="url", placeholder=placeholder
    )


API_KEY = Field("api_key", "API key", "secret", store="api_key")
CATEGORIES = Field(
    "categories",
    "Categories",
    "categories",
    default=[MAGAZINES],
    help="The indexer's categories to search. Load them from the indexer, or leave "
    "empty to use the categories set below the list (7010 is magazines).",
    store="categories",
)
API_PATH = Field(
    "api_path",
    "API path",
    default=DEFAULT_API_PATH,
    help="Path to the indexer's API, usually /api.",
    advanced=True,
)
PARAMETERS = Field(
    "additional_parameters",
    "Additional parameters",
    default="",
    help="Added to every call, for example &maxage=1000. PeriodiCool's own "
    "parameters (t, q, cat, apikey, extended) cannot be changed here.",
    advanced=True,
)


def _torrent_fields(advanced_goals: bool) -> tuple[Field, ...]:
    return (
        Field(
            "min_seeders",
            "Minimum seeders",
            "int",
            default=1,
            help="Torrents with fewer seeders are rejected.",
            advanced=True,
        ),
        Field(
            "seed_ratio",
            "Seed ratio",
            "decimal",
            default="",
            help="The ratio a torrent should reach before the client stops it. Empty "
            "uses the client's own.",
            advanced=advanced_goals,
        ),
        Field(
            "seed_time_min",
            "Seed time (minutes)",
            "int",
            default="",
            help="How long a torrent should seed before the client stops it. Empty uses "
            "the client's own.",
            advanced=advanced_goals,
        ),
        Field(
            "reject_blocklisted_hashes",
            "Reject blocklisted torrent hashes",
            "bool",
            default=False,
            help="If a torrent was blocklisted by hash, refuse it again when it comes "
            "from another indexer or under another name.",
            advanced=True,
        ),
    )


LIMITS = (
    Field(
        "daily_cap",
        "API calls per day",
        "int",
        default="",
        help="PeriodiCool stops at this number of calls a day. Empty means no limit.",
        advanced=True,
    ),
    Field(
        "grab_cap",
        "Downloads per day",
        "int",
        default="",
        help="PeriodiCool stops at this number of downloads a day. Empty means no limit.",
        advanced=True,
    ),
)


def _build_prowlarr(row, key, settings, common):
    from periodicool.prowlarr import ProwlarrClient

    return ProwlarrClient(row.url, key, **common)


def _build_newznab(row, key, settings, common):
    from periodicool.newznab import NewznabClient

    return NewznabClient(row.url, key, name=row.name, **_newznab_options(settings), **common)


def _build_torznab(row, key, settings, common):
    from periodicool.newznab import TorznabClient

    return TorznabClient(row.url, key, name=row.name, **_newznab_options(settings), **common)


def _newznab_options(settings: dict) -> dict:
    return {
        "api_path": settings.get("api_path") or DEFAULT_API_PATH,
        "extra_params": parse_parameters(settings.get("additional_parameters") or "")[0],
    }


def parse_parameters(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """`&maxage=1000&foo=bar` -> pairs, or an error for a reserved or broken key."""
    import urllib.parse

    text = (text or "").strip().lstrip("&?")
    if not text:
        return [], None
    pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    if not pairs or any("=" not in piece for piece in text.split("&") if piece):
        return [], "Write parameters as name=value, joined with &."
    for key, _ in pairs:
        if key.lower() in RESERVED_PARAMETERS:
            return [], f"{key} is set by PeriodiCool and cannot be changed here."
    return pairs, None


NEWZNAB_PRESETS = (
    Preset("DOGnzb", "https://api.dognzb.cr"),
    Preset("DrunkenSlug", "https://drunkenslug.com"),
    Preset("Nzb.life", "https://api.nzb.life"),
    Preset("NZBCat", "https://nzb.cat"),
    Preset("NZBFinder.ws", "https://nzbfinder.ws"),
    Preset("NZBgeek", "https://api.nzbgeek.info"),
    Preset("nzbplanet.net", "https://api.nzbplanet.net"),
    Preset("SimplyNZBs", "https://simplynzbs.com"),
    Preset("Tabula Rasa", "https://www.tabula-rasa.pw", "/api/v1/api"),
)

KINDS: dict[str, IndexerKind] = {
    k.kind: k
    for k in (
        IndexerKind(
            "prowlarr",
            "Prowlarr",
            "usenet",
            "Searches every indexer you set up in Prowlarr that carries magazines, in "
            "one call. Its torrent indexers are used only while a torrent client is set "
            "up; the torrent fields below apply to them.",
            (
                _url("Prowlarr address", "For example http://prowlarr:9696.", "http://"),
                API_KEY,
                CATEGORIES,
                *_torrent_fields(advanced_goals=True),
            ),
            _build_prowlarr,
            "prowlarr",
        ),
        IndexerKind(
            "newznab",
            "Newznab",
            "usenet",
            "One usenet indexer, added directly with its own API key and daily limits.",
            (
                _url("URL", "The indexer's site, for example https://indexer.example.", "https://"),
                API_PATH,
                API_KEY,
                CATEGORIES,
                PARAMETERS,
                *LIMITS,
            ),
            _build_newznab,
            "newznab",
            NEWZNAB_PRESETS,
        ),
        IndexerKind(
            "torznab",
            "Torznab",
            "torrent",
            "One torrent indexer (or Jackett), added directly. Searched only while a "
            "torrent client is set up.",
            (
                _url("URL", "The indexer's Torznab address.", "https://"),
                API_PATH,
                API_KEY,
                CATEGORIES,
                PARAMETERS,
                *_torrent_fields(advanced_goals=False),
                *LIMITS,
            ),
            _build_torznab,
            "torznab",
        ),
    )
}

INDEXER_KINDS = {k: v.label for k, v in KINDS.items()}
INDEXER_PROTOCOLS = {k: v.protocol for k, v in KINDS.items()}


def preset(kind: str, name: str) -> Preset | None:
    found = KINDS.get(kind)
    return next((p for p in (found.presets if found else ()) if p.name == name), None)


def defaults(kind: str) -> dict:
    return {f.name: f.default for f in KINDS[kind].fields if f.store == "settings"}
