# PeriodiCool

Self-hosted manager for magazines, in the style of Sonarr. A magazine is the series and an issue is the episode: PeriodiCool tracks titles, works out which issues should exist, finds releases through Prowlarr, sends them to SABnzbd, and keeps your library organized.

> **Status:** early development. The back end works from the command line; there is no web UI yet. See [CHANGELOG.md](CHANGELOG.md).

## No content, no indexers

PeriodiCool ships no content and no indexers. You bring your own Prowlarr instance, your own indexers and your own download client. PeriodiCool only talks to services you configure, and makes no other outbound calls (no telemetry, no CDNs).

## Planned features

- Track magazines with aliases, regional editions, frequency and numbering scheme.
- Generate expected issues from frequency and numbering, so Wanted and Missing work without a metadata provider.
- Find releases by RSS and search through Prowlarr, with rate limiting and an accept/reject reason for every candidate.
- Send to SABnzbd in PeriodiCool's own category, and only ever touch jobs PeriodiCool created.
- Import and organize into `{Title}/{Year}/{Title} - {YYYY-MM}.{ext}`, with a dry-run preview. Files are moved and renamed, never deleted or overwritten.
- Manual import queue for anything uncertain.

## Running in Docker

The image is `ghcr.io/electrichobo/periodicool` (amd64 and arm64). See [examples/docker-compose.yml](examples/docker-compose.yml).

- The app runs as `PUID`:`PGID` (default 99:100, Unraid's nobody:users), never as root; the container refuses `PUID=0`. Only `/config` is chowned; `/library` and `/downloads` are left as they are.
- Mount `/config` (database), `/library` (magazines), `/downloads` (the download client's completed folder for PeriodiCool's category) and optionally `/import`.
- Run commands inside the container, for example: `docker exec -it periodicool python -m periodicool scan`.
- First run on an existing library: `scan` shows a preview only. Read it before `apply`.

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
python -m periodicool config set prowlarr.url http://prowlarr:9696
python -m periodicool indexers        # per-indexer limits and what is left today
python -m periodicool search 3        # every candidate with its accept/reject reasons
python -m periodicool rss             # one RSS sync now
python -m periodicool wanted          # wanted and missing issues
python -m periodicool config set sabnzbd.url http://sabnzbd:8080
python -m periodicool config set sabnzbd.category magazines
python -m periodicool sab test        # SABnzbd reachable and the category exists
python -m periodicool grab 42         # send release 42 to SABnzbd
python -m periodicool monitor         # check downloads, import finished ones
python -m periodicool downloads       # grabs and their status
```

PeriodiCool only adds jobs in its own SABnzbd category (create it in SABnzbd first) and only ever reads the jobs it created. If SABnzbd sees its completed folder at a different path than PeriodiCool does, map it: `config set sabnzbd.path_map '[["/data/complete/magazines", "/downloads"]]'`. Automatic grabbing is off until `config set grab.auto true`.

The Prowlarr API key comes from the `PROWLARR_API_KEY` environment variable, never the database. Indexer API limits are whatever you set per indexer in Prowlarr; PeriodiCool reads them and stays within them.

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
| `PROWLARR_API_KEY` | unset | Prowlarr API key |
| `SABNZBD_API_KEY` | unset | SABnzbd API key |
| `PERIODICOOL_JOBS` | `on` | `off` disables background jobs |

## License

[GPL-3.0](LICENSE).
