# PeriodiCool

Self-hosted manager for magazines, in the style of Sonarr. A magazine is the series and an issue is the episode: PeriodiCool tracks titles, works out which issues should exist, finds releases through Prowlarr or Newznab indexers, sends them to SABnzbd, and keeps your library organized.

> **Status:** early development. The back end works from the command line; there is no web UI yet. See [CHANGELOG.md](CHANGELOG.md).

## No content, no indexers

PeriodiCool ships no content and no indexers. You bring your own Prowlarr instance, your own indexers and your own download client. PeriodiCool only talks to services you configure, and makes no other outbound calls (no telemetry, no CDNs).

## Planned features

- Track magazines with aliases, regional editions, frequency and numbering scheme.
- Generate expected issues from frequency and numbering, so Wanted and Missing work without a metadata provider.
- Find releases by RSS and search through any number of indexers (Prowlarr, or Newznab indexers added directly), with rate limiting and an accept/reject reason for every candidate.
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
python -m periodicool indexer add prowlarr Prowlarr http://prowlarr:9696   # asks for the API key
python -m periodicool indexers        # per-indexer limits and what is left today
python -m periodicool search 3        # every candidate with its accept/reject reasons
python -m periodicool rss             # one RSS sync now
python -m periodicool wanted          # wanted and missing issues
python -m periodicool client add sabnzbd SABnzbd http://sabnzbd:8080 --category magazines
python -m periodicool indexer list    # or: client list, indexer remove ID
python -m periodicool sab test        # each download client reachable and its category exists
python -m periodicool grab 42         # send release 42 to the download client
python -m periodicool monitor         # check downloads, import finished ones
python -m periodicool downloads       # grabs and their status
```

Indexers and download clients are lists, as in Sonarr and Radarr: add, test, enable, order and remove them in Settings, Indexers and Settings, Download clients (or with the `indexer` and `client` commands). An indexer is either a Prowlarr, which searches every usenet indexer you set up in it, or a single Newznab indexer added directly with its own daily limits. Searches run on every enabled indexer; one that is rate limited backs off on its own while the others carry on.

PeriodiCool only adds jobs in each client's own category (create it in the client first) and only ever reads the jobs it created. If the client sees its completed folder at a different path than PeriodiCool does, add a remote path mapping in that client's settings. Automatic grabbing is off until `config set grab.auto true`.

API keys are stored in PeriodiCool's database with their entry. They are never shown again after saving (an empty key field keeps the saved key), are scrubbed from the logs, and are included in backups, so keep backups private. The command line asks for a key or reads it from an environment variable you name (`--api-key-env`), never from the command line itself. Indexer API limits are whatever you set per indexer in Prowlarr (or on a Newznab entry); PeriodiCool reads them and stays within them.

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
| `PROWLARR_API_KEY` | unset | Optional. API key for the first Prowlarr entry when it has none saved |
| `SABNZBD_API_KEY` | unset | Optional. API key for the first SABnzbd entry when it has none saved |
| `PERIODICOOL_JOBS` | `on` | `off` disables background jobs |
| `PERIODICOOL_SECRET` | unset | Signing secret for CSRF tokens. When unset, one is generated into `/config/secret.key` |

## Login

The first visit to the web UI asks you to create the one admin login. Sessions last 30 days and renew while you use them. After 5 failed logins from one address, that address has to wait, starting at 30 seconds and doubling up to 15 minutes.

The setting `auth.mode` controls who needs to log in:

| Mode | Who logs in |
| - | - |
| `required` (default) | Everyone |
| `local` | Only visitors from outside private and loopback addresses. `X-Forwarded-For` is ignored unless `auth.trust_proxy` is on |
| `off` | Nobody. Only use this on a network you trust |

Locked out? Run this inside the container and enter a new password; it also logs out every session:

```
python -m periodicool user reset-password
```

An API key is generated into `/config/api.key` on first start. For now only the calendar feed uses it.

## Metadata lookups (off by default)

PeriodiCool can ask three public sources about your magazines. Each one is off until you turn it on in Settings, Metadata. Turning them on is worth it: they help recognize magazines and editions, check frequencies, and find past issues.

| Source | What it gives | What is sent |
| - | - | - |
| Library of Congress catalog | Title, other titles, publisher, frequency, first issue | The ISSN (or the title when there is none) |
| Wikidata | Title, other names, ISSN, start year, language, country, website | The ISSN (or the title when there is none) |
| Internet Archive | Lists of past issues, read from item titles | The title |

Requests go straight from your PeriodiCool to the source; there is no PeriodiCool server. Answers are kept in your database for 30 days. Nothing about a magazine changes until you tick it and press Apply in its Metadata pop-up. Other names found for magazines that have an ISSN also help recognize files in your library; searches and grabs only ever use your own titles. Only metadata is read: no issues or files are downloaded from these sources.

## Running commands in the container

`docker exec periodicool python -m periodicool <command>` works for every command (`scan`, `apply`, `user reset-password` and the rest). `docker exec` starts as root, so PeriodiCool switches to `PUID`:`PGID` before doing anything; files and folders it creates belong to the same user as the server's.

## Reading PDFs

When a file name is not enough to place a file, PeriodiCool reads the text of the PDF's first pages (two by default) with [pypdf](https://pypi.org/project/pypdf/) and looks for an ISSN, a cover date and an issue number. It never sends anything anywhere, gives up after a few seconds, and skips encrypted, damaged or image-only files. A file only moves when the PDF's ISSN matches a magazine or a PDF clue agrees with the name; otherwise it waits in the manual import queue with a suggestion. Set a magazine's ISSN (printed near the barcode or masthead) to make this reliable and to catch wrong editions on import. Turn it off or change the page count in Settings, Media management.

## License

[GPL-3.0](LICENSE).
