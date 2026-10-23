"""Every download-client kind PeriodiCool can drive, with the fields its Settings
pop-up asks for (Sonarr's, per client). The registry drives the Add picker, the
form (rendering, parsing, write-only secrets) and building a working client.

Fields come prefilled with the client's usual defaults; the user fills in the rest.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"  # text, int, bool, select, secret
    default: object = ""
    help: str = ""
    required: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    # Where the value lives: settings (JSON), or one of the row's own columns.
    store: str = "settings"  # settings, api_key, password, username, category
    advanced: bool = False


@dataclass(frozen=True)
class ClientKind:
    kind: str
    label: str
    protocol: str  # usenet or torrent
    info: str
    fields: tuple[Field, ...]
    build: Callable  # (url, row, settings, secret, transport) -> client
    transport_key: str
    # A secret the client cannot work without (SABnzbd's API key); others may be empty.
    required_secret: str | None = None
    defaults: dict = field(default_factory=dict)


def _connection(
    port: int, url_base: str = "", base_label: str = "URL base", base_help: str = ""
) -> tuple[Field, ...]:
    return (
        Field(
            "host",
            "Host",
            default="",
            required=True,
            help="Name or address of the client, for example sabnzbd or 192.0.2.10.",
        ),
        Field("port", "Port", "int", port),
        Field("use_ssl", "Use SSL", "bool", False, "Connect over HTTPS."),
        Field(
            "url_base",
            base_label,
            default=url_base,
            help=base_help
            or "Only when the client runs under a path, for example behind a reverse proxy.",
        ),
    )


CATEGORY_HELP = (
    "PeriodiCool adds its jobs only with this {what}, and only ever reads or removes its "
    "own jobs. Create it in the client first."
)


def _category(what: str = "category", required: bool = True) -> Field:
    return Field(
        "category",
        what.capitalize(),
        default="",
        required=required,
        help=CATEGORY_HELP.format(what=what),
        store="category",
    )


def _priorities(choices: tuple[tuple[str, str], ...], default: str) -> tuple[Field, ...]:
    return (
        Field(
            "recent_priority",
            "Recent priority",
            "select",
            default,
            "For issues whose cover date is in the last 14 days.",
            choices=choices,
        ),
        Field(
            "older_priority",
            "Older priority",
            "select",
            default,
            "For back issues.",
            choices=choices,
        ),
    )


QUEUE_ORDER = (("last", "Last"), ("first", "First"))
SAB_PRIORITIES = (
    ("-100", "Default"),
    ("-1", "Low"),
    ("0", "Normal"),
    ("1", "High"),
    ("2", "Force"),
)
NZBGET_PRIORITIES = (
    ("-100", "Very low"),
    ("-50", "Low"),
    ("0", "Normal"),
    ("50", "High"),
    ("100", "Very high"),
    ("900", "Force"),
)
RTORRENT_PRIORITIES = (("0", "Very low"), ("1", "Low"), ("2", "Normal"), ("3", "High"))

POST_IMPORT = Field(
    "post_import_category",
    "Post-import category",
    default="",
    help="Optional. After import, PeriodiCool moves its own torrent to this category, "
    "for tools that manage seeding. Empty leaves it where it is.",
)
REMOVE_COMPLETED = Field(
    "remove_completed",
    "Remove completed",
    "bool",
    True,
    "After import, remove PeriodiCool's own finished job from the client's history. "
    "Files are never touched.",
)


def _build_sab(url, row, settings, secret, transport):
    from periodicool.clients.sabnzbd import SabnzbdClient

    return SabnzbdClient(url, secret, transport=transport)


def _build_qbit(url, row, settings, secret, transport):
    from periodicool.clients.qbittorrent import QbittorrentClient

    return QbittorrentClient(url, row.username or "", row.password or "", transport=transport)


def _build_nzbget(url, row, settings, secret, transport):
    from periodicool.clients.nzbget import NzbgetClient

    return NzbgetClient(url, row.username or "", row.password or "", transport=transport)


def _build_transmission(url, row, settings, secret, transport):
    from periodicool.clients.transmission import TransmissionClient

    return TransmissionClient(url, row.username or "", row.password or "", transport=transport)


def _build_deluge(url, row, settings, secret, transport):
    from periodicool.clients.deluge import DelugeClient

    return DelugeClient(url, row.password or "", transport=transport)


def _build_rtorrent(url, row, settings, secret, transport):
    from periodicool.clients.rtorrent import RtorrentClient

    return RtorrentClient(url, row.username or "", row.password or "", transport=transport)


USERNAME = Field("username", "Username", default="", store="username")
PASSWORD = Field("password", "Password", "secret", store="password")

KINDS: dict[str, ClientKind] = {
    k.kind: k
    for k in (
        ClientKind(
            "sabnzbd",
            "SABnzbd",
            "usenet",
            "Usenet downloads. PeriodiCool reads SABnzbd's categories, adds jobs in its own "
            "category, and reads or removes only those jobs.",
            (
                *_connection(8080),
                Field(
                    "api_key",
                    "API key",
                    "secret",
                    store="api_key",
                    help="SABnzbd, Config, General, Security.",
                ),
                USERNAME,
                PASSWORD,
                _category(),
                *_priorities(SAB_PRIORITIES, "-100"),
                REMOVE_COMPLETED,
            ),
            _build_sab,
            "sab",
            required_secret="api_key",
        ),
        ClientKind(
            "nzbget",
            "NZBGet",
            "usenet",
            "Usenet downloads over NZBGet's JSON-RPC. PeriodiCool adds jobs in its own "
            "category and reads or removes only those jobs.",
            (
                *_connection(6789),
                Field("username", "Username", default="nzbget", store="username"),
                PASSWORD,
                _category(),
                *_priorities(NZBGET_PRIORITIES, "0"),
                Field("add_paused", "Add paused", "bool", False),
                REMOVE_COMPLETED,
            ),
            _build_nzbget,
            "nzbget",
        ),
        ClientKind(
            "qbittorrent",
            "qBittorrent",
            "torrent",
            "Torrents. Files are hard-linked into the library (or copied across disks), "
            "never moved, so they keep seeding. Seed goals come from each indexer entry; "
            "qBittorrent decides what happens when they are met.",
            (
                *_connection(8080),
                USERNAME,
                PASSWORD,
                _category(),
                POST_IMPORT,
                *_priorities(QUEUE_ORDER, "last"),
                Field(
                    "initial_state",
                    "Initial state",
                    "select",
                    "start",
                    choices=(
                        ("start", "Start"),
                        ("force_start", "Force start"),
                        ("pause", "Pause"),
                    ),
                ),
            ),
            _build_qbit,
            "qbit",
        ),
        ClientKind(
            "transmission",
            "Transmission",
            "torrent",
            "Torrents over Transmission's RPC. Transmission has no categories, so "
            "PeriodiCool's category is a label on its own torrents.",
            (
                *_connection(9091, "/transmission/"),
                USERNAME,
                PASSWORD,
                _category("label"),
                Field(
                    "directory",
                    "Directory",
                    default="",
                    help="Optional. Where Transmission saves PeriodiCool's torrents, as "
                    "Transmission sees it. Empty uses its default.",
                ),
                *_priorities(QUEUE_ORDER, "last"),
                Field("add_paused", "Add paused", "bool", False),
            ),
            _build_transmission,
            "transmission",
        ),
        ClientKind(
            "deluge",
            "Deluge",
            "torrent",
            "Torrents through Deluge's Web UI. The category is a Deluge label, so the "
            "Label plugin must be turned on.",
            (
                *_connection(8112),
                PASSWORD,
                _category("label"),
                POST_IMPORT,
                *_priorities(QUEUE_ORDER, "last"),
                Field("add_paused", "Add paused", "bool", False),
            ),
            _build_deluge,
            "deluge",
        ),
        ClientKind(
            "rtorrent",
            "rTorrent",
            "torrent",
            "Torrents over rTorrent's XML-RPC (also through ruTorrent). The category is "
            "rTorrent's label (custom1), as ruTorrent shows it.",
            (
                *_connection(
                    8080,
                    "RPC2",
                    "URL path",
                    "rTorrent's XML-RPC path, usually RPC2. Through ruTorrent: "
                    "plugins/httprpc/action.php.",
                ),
                USERNAME,
                PASSWORD,
                _category("label"),
                POST_IMPORT,
                Field(
                    "directory",
                    "Directory",
                    default="",
                    help="Optional. Where rTorrent saves PeriodiCool's torrents, "
                    "as rTorrent sees it.",
                ),
                *_priorities(RTORRENT_PRIORITIES, "2"),
                Field("add_paused", "Add stopped", "bool", False),
            ),
            _build_rtorrent,
            "rtorrent",
        ),
    )
}

CLIENT_KINDS = {k: v.label for k, v in KINDS.items()}
CLIENT_PROTOCOLS = {k: v.protocol for k, v in KINDS.items()}


def defaults(kind: str) -> dict:
    """The form values of a new entry of this kind."""
    out = {}
    for f in KINDS[kind].fields:
        if f.type != "secret":
            out[f.name] = f.default
    return out


def compose_url(settings: dict) -> str:
    """The client's base address from host, port, SSL and URL base."""
    host = str(settings.get("host") or "").strip()
    if not host:
        return ""
    ssl = bool(settings.get("use_ssl"))
    port = settings.get("port")
    scheme = "https" if ssl else "http"
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port and int(port) != (443 if ssl else 80):
        netloc += f":{int(port)}"
    base = str(settings.get("url_base") or "").strip().strip("/")
    return f"{scheme}://{netloc}" + (f"/{base}" if base else "")


def split_url(url: str) -> dict:
    """http://sab:8080/sabnzbd -> host, port, use_ssl, url_base (the inverse of compose_url)."""
    import urllib.parse

    parts = urllib.parse.urlsplit((url or "").strip())
    ssl = parts.scheme == "https"
    return {
        "host": parts.hostname or "",
        "port": parts.port or (443 if ssl else 80),
        "use_ssl": ssl,
        "url_base": parts.path.rstrip("/"),
    }
