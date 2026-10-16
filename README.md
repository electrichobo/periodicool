# PeriodiCool

Self-hosted manager for magazines, in the style of Sonarr. A magazine is the series and an issue is the episode: PeriodiCool tracks titles, works out which issues should exist, finds releases through Prowlarr, sends them to SABnzbd, and keeps your library organized.

> **Status:** early development. Nothing here is usable yet. See [CHANGELOG.md](CHANGELOG.md).

## No content, no indexers

PeriodiCool ships no content and no indexers. You bring your own Prowlarr instance, your own indexers and your own download client. PeriodiCool only talks to services you configure, and makes no other outbound calls (no telemetry, no CDNs).

## Planned features

- Track magazines with aliases, regional editions, frequency and numbering scheme.
- Generate expected issues from frequency and numbering, so Wanted and Missing work without a metadata provider.
- Find releases by RSS and search through Prowlarr, with rate limiting and an accept/reject reason for every candidate.
- Send to SABnzbd in PeriodiCool's own category, and only ever touch jobs PeriodiCool created.
- Import and organize into `{Title}/{Year}/{Title} - {YYYY-MM}.{ext}`, with a dry-run preview. Files are moved and renamed, never deleted or overwritten.
- Manual import queue for anything uncertain.

## Development

Requires Python 3.14.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
pytest
ruff check .
```

Run the app against local test folders (never point a dev build at a real library):

```bash
PERIODICOOL_CONFIG_DIR=./dev-data/config python -m periodicool
```

Then open `http://localhost:8383/health`.

### Command line (until the web UI exists)

```bash
python -m periodicool magazines import examples/magazines.json
python -m periodicool scan            # preview only, nothing moves
python -m periodicool apply 1         # carry out scan 1
python -m periodicool queue           # files waiting for review
python -m periodicool resolve ITEM_ID MAGAZINE_ID 2023-11
python -m periodicool skip ITEM_ID
```

`scan` never touches files. Always read the preview before running `apply`, and try it on a copy of your library first.

### Environment variables

Only paths, process settings and secrets come from the environment. Everything else lives in the database and is edited in the UI.

| Variable | Default | Purpose |
| - | - | - |
| `PERIODICOOL_CONFIG_DIR` | `/config` | Database and app data |
| `PERIODICOOL_LIBRARY_DIR` | `/library` | Magazine library |
| `PERIODICOOL_DOWNLOADS_DIR` | `/downloads` | Download client's completed folder for PeriodiCool's category |
| `PERIODICOOL_IMPORT_DIR` | unset | Optional drop-in import folder |
| `PERIODICOOL_HOST` | `0.0.0.0` | Bind address |
| `PERIODICOOL_PORT` | `8383` | Port |
| `PUID` / `PGID` | `99` / `100` | User and group the container runs as |
| `UMASK` | `002` | File creation mask |
| `TZ` | `UTC` | Time zone |

## License

[GPL-3.0](LICENSE).
