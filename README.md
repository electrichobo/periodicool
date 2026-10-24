<p align="center"><img src="unraid/periodicool.png" alt="PeriodiCool" width="160"></p>

# PeriodiCool

Self-hosted manager for magazines, in the style of Sonarr. A magazine is the series and an issue is the episode: PeriodiCool tracks titles, works out which issues should exist, finds releases through your indexers, sends them to your download client, and keeps your library organized.

> **Status:** young but working (v0.2.0): the web UI, the background jobs, the REST API and the Docker image are all in place. It runs on the maintainer's Unraid server with Prowlarr and SABnzbd; the other download clients follow their documented APIs and are waiting for real-world reports. See [CHANGELOG.md](CHANGELOG.md).

## Getting started, the short version

Think of PeriodiCool as a librarian for your magazine subscriptions. You tell it which magazines you read. It knows roughly when each new issue comes out, watches your indexers for it, asks your download client to fetch it, and files it neatly on your shelf (your library folder) under a clean name. Back issues work the same way.

You need three things it does not include: a place to look (an indexer, usually through Prowlarr), something to download with (such as SABnzbd or qBittorrent), and a folder for the magazines.

1. **Run it.** On Unraid, install PeriodiCool from Community Apps (or the template in [`unraid/`](unraid/)). Anywhere else, use Docker with [examples/docker-compose.yml](examples/docker-compose.yml): point `/config` at an app-data folder, `/library` at your magazines, and mount your downloads folder the same way your download client sees it.
2. **Open it** at `http://your-server:8383` and create your login.
3. **Add an indexer** under Settings, Indexers (Prowlarr, or a Newznab or Torznab indexer).
4. **Add a download client** under Settings, Download clients, with its own category (for example `magazines`; create it in the client first).
5. **Add a magazine.** Press Add magazine, type a title, pick the right match and choose what to watch for: new issues only, issues from a year onward, or everything.

That's it. PeriodiCool checks RSS every 15 minutes and looks for wanted issues on a schedule. Press Search monitored on a magazine's page to look right away.

Already have magazines on disk? Run the library scan first: it shows a preview of where everything would go, and nothing moves until you say so.

## Documentation

- [Configuration](docs/configuration.md): environment variables, every Settings tab, backups.
- [API and webhooks](docs/api.md): the REST API under `/api/v1`, commands, the webhook payload.
- [Unraid](docs/unraid.md): installing from the template, paths, first start, common problems.

## Features

**Magazines and issues**
- Track magazines with other names (aliases), regional editions, languages, ISSN and tags.
- Frequency (weekly to quarterly, seasonal, 13 a year, irregular) and numbering (by date, issue number, volume or season).
- Expected issues are worked out from frequency and numbering, so Wanted and Missing work without any metadata service.
- Choose what to monitor: new issues only, from a year onward, or all; per issue too.
- Add magazine is a search: it groups what your indexers find and, when you allow it, matches from public catalogs.
- Calendar view and an iCal feed of upcoming issues.

**Finding releases**
- Prowlarr, Newznab and Torznab indexers, as a list like Sonarr's, with Sonarr's per-indexer fields and presets.
- RSS first; searches are rate-limited, cached and back off on their own. Daily API and grab limits are respected.
- Interactive search lists every candidate with the reason it was accepted or rejected.
- A release-name parser built for magazines: month names, seasons, issue and volume numbers, specials, regional editions, True PDF versus scans, multi-issue packs.
- Format profiles (PDF, EPUB, CBZ, CBR), size and age limits, must-contain and must-not-contain words, a blocklist.

**Downloading**
- SABnzbd, NZBGet, qBittorrent, Transmission, Deluge, rTorrent, and Usenet and Torrent Blackhole folders.
- PeriodiCool only adds jobs in its own category and only touches jobs it created.
- Failed downloads are blocklisted and the next best release is tried.
- Torrents keep seeding: imports hard-link or copy, never move. Seed goals per indexer.

**Library**
- One layout for everything: `{Title}/{Year}/{Title} - {YYYY-MM}.{ext}` by default, configurable.
- Library scan with a full preview, an optional drop-in import folder, and a manual import queue for anything uncertain.
- Files are moved and renamed, never deleted or overwritten; every move is logged.
- Reads the first pages of a PDF (ISSN, cover date, issue number) when the file name is not enough.
- Covers from each magazine's newest issue.

**Running it**
- Web UI in a cassette-futurism look, with light and dark themes and keyboard navigation.
- Login (or local-network only), CSRF protection, write-only secrets that never reach the logs.
- Health and status page, task list, live log, backups and restore.
- REST API in Sonarr's style, and webhook notifications for grabs, imports, failures and health.
- Opt-in metadata lookups (Library of Congress, Wikidata, Internet Archive), all off by default.
- Docker image for amd64 and arm64 that never runs as root, and an Unraid template.

## Contributing

PeriodiCool is open source under the GPL, and help is welcome. Fork it, try it, break it, and send a pull request.

Good places to start:
- **Real download clients.** NZBGet, qBittorrent, Transmission, Deluge, rTorrent and the Blackholes have only been tested against fakes. A report from a real setup, working or not, is very useful.
- **Release names.** The parser learns from real names. If PeriodiCool misreads one, open a [parser report](.github/ISSUE_TEMPLATE/parser_report.yml) with the name.
- **Magazines with odd schedules.** Double issues, specials, regional editions and irregular titles all teach it something.
- **Bugs and ideas.** Use the [bug report](.github/ISSUE_TEMPLATE/bug_report.yml) or [feature request](.github/ISSUE_TEMPLATE/feature_request.yml) forms.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) first: it covers the setup and the safety rules that keep people's libraries and download clients safe. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## No content, no indexers

PeriodiCool ships no content and no indexers. You bring your own indexers and your own download client. PeriodiCool only talks to services you configure (and, if you turn them on, the public metadata sources below). It makes no other outbound calls: no telemetry, no CDNs.

## In more detail

### Docker

The image is `ghcr.io/electrichobo/periodicool` (amd64 and arm64). See [examples/docker-compose.yml](examples/docker-compose.yml), or [docs/unraid.md](docs/unraid.md) on Unraid.

- The app runs as `PUID`:`PGID` (default 99:100, Unraid's nobody:users), never as root; the container refuses `PUID=0`. Only `/config` is chowned; `/library` and the downloads folder are left as they are.
- Mount `/config` (database), `/library` (magazines), the download client's folder (at the same path the client uses, set with `PERIODICOOL_DOWNLOADS_DIR`) and optionally `/import`.
- `docker exec periodicool python -m periodicool <command>` works for every command. `docker exec` starts as root, so PeriodiCool switches to `PUID`:`PGID` before doing anything.
- Every environment variable is listed in [docs/configuration.md](docs/configuration.md).

### Indexers

Indexers are a list, as in Sonarr and Radarr: add, test, order and switch them on or off under Settings, Indexers. An entry is either a Prowlarr, which searches every indexer you set up in it, or a single Newznab or Torznab indexer added directly with its own daily limits. A rate-limited indexer backs off on its own while the others carry on.

- Enable RSS, Enable Automatic Search and Enable Interactive Search are separate switches. Automatic means scheduled, Wanted and Search-button searches. Interactive means the interactive search and the Add magazine search.
- Load categories reads the categories the indexer reports and ticks Magazines (7010).
- Show advanced reveals the API path, additional parameters, daily limits and the torrent options.
- An indexer can name its own download client; its releases then go only there.
- Add lists Sonarr's usenet indexer presets. A preset only fills in the address; you add your own key, and nothing is contacted until you test or save.

### Download clients

Settings, Download clients, Add lists the supported clients under Usenet (SABnzbd, NZBGet, Usenet Blackhole) and Torrents (qBittorrent, Transmission, Deluge, rTorrent, Torrent Blackhole). Each form has that client's own fields, as in Sonarr: host, port, SSL, URL base, credentials, category or label, recent and older priority, add paused, and where the client has them, a directory, a post-import category and remove completed (our finished usenet job leaves the client's history; files are never touched).

- Transmission and rTorrent have no categories: PeriodiCool marks its torrents with a label instead (rTorrent's label is `d.custom1`, which ruTorrent shows).
- Deluge needs its Label plugin turned on, and the label created in Deluge.
- rTorrent's URL path is the XML-RPC path, usually `RPC2`.
- **Blackholes:** PeriodiCool writes the `.nzb` or `.torrent` into a drop folder; your downloader picks it up and leaves the finished download in the watch folder, which PeriodiCool imports once it stops changing. Only items named after what PeriodiCool dropped are looked at.
- If a client sees its download folder at a different path than PeriodiCool does, add a remote path mapping under Settings, Download clients.
- Automatic grabbing is off until you turn it on in Settings, Download clients.

### Torrents

Add a torrent client, and either Prowlarr (its torrent indexers are used once a torrent client exists) or a Torznab indexer. Each indexer entry can set a seed ratio, a seed time and a minimum number of seeders. The seed goals go to the client with each torrent, and the client decides what happens when they are met; PeriodiCool never changes the client's own settings.

A torrent keeps seeding from the client's folder, so PeriodiCool hard-links the finished file into the library and never moves or deletes the original. Hard links only work within one filesystem: when the library is on another disk or share, the file is copied instead. Torrents and usenet releases compete on format and quality alone, then on indexer priority.

### Keys and secrets

API keys and passwords are stored in PeriodiCool's database with their entry. They are never shown again after saving (an empty field keeps the saved one), are scrubbed from the logs, and are included in backups, so keep backups private.

### Login

The first visit asks you to create the one admin login. Sessions last 30 days and renew while you use them. After 5 failed logins from one address, that address has to wait, starting at 30 seconds and doubling up to 15 minutes.

Settings, General, Login controls who needs to log in: everyone (the default), only visitors from outside your local network, or nobody (only on a network you trust).

Locked out? Run this inside the container and enter a new password; it also logs out every session:

```
python -m periodicool user reset-password
```

### API and webhooks

An API key is generated on first start; Settings, General shows it. The REST API under `/api/v1` (Sonarr's style: `X-Api-Key`, camelCase JSON, commands) and the calendar feed use it. Settings, Connect posts events (grab, import, failures, health and more) as JSON to an address you choose. Both are described in [docs/api.md](docs/api.md).

### Metadata lookups (off by default)

PeriodiCool can ask three public sources about your magazines. Each one is off until you turn it on in Settings, Metadata. Turning them on is worth it: they help recognize magazines and editions, check frequencies, and find past issues.

| Source | What it gives | What is sent |
| - | - | - |
| Library of Congress catalog | Title, other titles, publisher, frequency, first issue | The ISSN (or the title when there is none) |
| Wikidata | Title, other names, ISSN, start year, language, country, website | The ISSN (or the title when there is none) |
| Internet Archive | Lists of past issues, read from item titles | The title |

Requests go straight from your PeriodiCool to the source; there is no PeriodiCool server. Answers are kept in your database for 30 days. Nothing about a magazine changes until you tick it and press Apply in its Metadata pop-up. Only metadata is read: no issues or files are downloaded from these sources.

### Reading PDFs

When a file name is not enough to place a file, PeriodiCool reads the text of the PDF's first pages (two by default) with [pypdf](https://pypi.org/project/pypdf/) and looks for an ISSN, a cover date and an issue number. It never sends anything anywhere, gives up after a few seconds, and skips encrypted, damaged or image-only files. A file only moves when the PDF's ISSN matches a magazine or a PDF clue agrees with the name; otherwise it waits in the manual import queue with a suggestion. Set a magazine's ISSN (printed near the barcode or masthead) to make this reliable. Turn it off or change the page count in Settings, Media management.

### Command line

Everything is in the web UI, but the command line is handy for scripts and for fixing things inside the container:

```bash
python -m periodicool magazines import examples/magazines.json
python -m periodicool scan            # preview only, nothing moves
python -m periodicool apply 1         # carry out scan 1
python -m periodicool queue           # files waiting for review
python -m periodicool search 3        # every candidate with its accept/reject reasons
python -m periodicool rss             # one RSS sync now
python -m periodicool wanted          # wanted and missing issues
python -m periodicool indexer list    # or: client list, indexer add, client add
python -m periodicool notify test 1   # send a test webhook event
python -m periodicool user reset-password
```

## Development

Requires Python 3.14.

```bash
python -m venv .venv
.venv/bin/pip install -c requirements.lock -e ".[dev]"      # Windows: .venv\Scripts\pip
pytest
ruff check .
```

Run the app against local test folders (never point a dev build at a real library):

```bash
PERIODICOOL_CONFIG_DIR=./dev-data/config PERIODICOOL_LIBRARY_DIR=./dev-data/library PERIODICOOL_DOWNLOADS_DIR=./dev-data/downloads python -m periodicool
```

Then open `http://localhost:8383`.

## License

[GPL-3.0](LICENSE). Fork it, change it, share it; keep it open.
