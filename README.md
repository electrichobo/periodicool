# PeriodiCool

Self-hosted manager for magazines, in the style of Sonarr. A magazine is the series and an issue is the episode: PeriodiCool tracks titles, works out which issues should exist, finds releases through Prowlarr, Newznab or Torznab indexers, sends them to your download client, and keeps your library organized.

> **Status:** early development. The back end works from the command line; there is no web UI yet. See [CHANGELOG.md](CHANGELOG.md).

## No content, no indexers

PeriodiCool ships no content and no indexers. You bring your own Prowlarr instance, your own indexers and your own download client. PeriodiCool only talks to services you configure, and makes no other outbound calls (no telemetry, no CDNs).

## Planned features

- Track magazines with aliases, regional editions, frequency and numbering scheme.
- Generate expected issues from frequency and numbering, so Wanted and Missing work without a metadata provider.
- Find releases by RSS and search through any number of indexers (Prowlarr, or Newznab indexers added directly), with rate limiting and an accept/reject reason for every candidate.
- Send to SABnzbd, NZBGet, qBittorrent, Transmission, Deluge or rTorrent in PeriodiCool's own category (or label), and only ever touch jobs PeriodiCool created.
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

Indexers and download clients are lists, as in Sonarr and Radarr: add, test, enable, order and remove them in Settings, Indexers and Settings, Download clients (or with the `indexer` and `client` commands). An indexer is either a Prowlarr, which searches every indexer you set up in it, or a single Newznab or Torznab indexer added directly with its own daily limits. Searches run on every enabled indexer; one that is rate limited backs off on its own while the others carry on.

The indexer form follows Sonarr's:

- Enable RSS, Enable Automatic Search and Enable Interactive Search are separate switches. Automatic means scheduled, Wanted and Search-button searches. Interactive means the interactive search and the Add magazine search.
- Load categories reads the categories the indexer reports and ticks Magazines (7010).
- Show advanced reveals the API path, additional parameters, daily limits and the torrent options.
- An indexer can name its own download client; its releases then go only there.
- Add lists Sonarr's usenet indexer presets. A preset only fills in the address; you add your own key, and nothing is contacted until you test or save.
- For torrents, "Reject blocklisted torrent hashes" refuses a torrent whose hash was blocklisted, under any name. The same torrent is never sent twice.

### Download clients

Settings, Download clients, Add lists the supported clients under Usenet (SABnzbd, NZBGet, Usenet Blackhole) and Torrents (qBittorrent, Transmission, Deluge, rTorrent, Torrent Blackhole). Each form has that client's own fields, as in Sonarr: host, port, SSL, URL base, credentials, category or label, recent and older priority, add paused, and where the client has them, a directory, a post-import category (our torrent moves there after import) and remove completed (our finished usenet job leaves the client's history; files are never touched). Recent means the issue's cover date is within the last 14 days.

Only SABnzbd has been checked against a real install so far. The others follow each client's documented API and are tested against fakes; reports from real setups are welcome. Notes per client:

- Transmission and rTorrent have no categories: PeriodiCool marks its torrents with a label instead (rTorrent's label is `d.custom1`, which ruTorrent shows).
- Deluge needs its Label plugin turned on, and the label created in Deluge.
- rTorrent's URL path is the XML-RPC path, usually `RPC2`.
- Seed time goes to qBittorrent only; Transmission and Deluge take the seed ratio; rTorrent keeps its own ratio groups.
- **Usenet and Torrent Blackhole**, for a downloader PeriodiCool cannot talk to:
  - **How it works:** PeriodiCool writes the `.nzb` or `.torrent` into the drop folder. Your downloader picks it up and leaves the finished download in the watch folder. PeriodiCool imports it once it has not changed for 30 seconds.
  - **Folders:** both are given as PeriodiCool sees them, so no path mapping is needed. The watch folder must be inside the downloads folder.
  - **Matching:** only items named after what PeriodiCool dropped are looked at. Nothing in the watch folder is deleted except a usenet job's leftovers after import, as with SABnzbd.
  - **What a blackhole can't report:** progress or failures; it has no category either.
  - **Torrent Blackhole:** torrents are always hard-linked or copied, never moved. A magnet-only release needs "Save magnet files" turned on.

### Torrents

Add a torrent client under Settings, Download clients, and either Prowlarr (its torrent indexers are used once a torrent client exists) or a Torznab indexer under Settings, Indexers. Each indexer entry can set a seed ratio, a seed time and a minimum number of seeders. The seed goals go to the client with each torrent, and the client applies its own action when they are met; PeriodiCool never changes the client's settings.

A torrent keeps seeding from the client's folder, so PeriodiCool hard-links the finished file into the library and never moves or deletes the original. Hard links only work within one filesystem: when the library is on another disk or share, the file is copied instead, which uses the space twice. Torrents and usenet releases compete on format and quality alone, then on indexer priority.

PeriodiCool only adds jobs in each client's own category (create it in the client first) and only ever reads the jobs it created. If the client sees its completed folder at a different path than PeriodiCool does, add a remote path mapping under Settings, Download clients. Automatic grabbing is off until `config set grab.auto true`.

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
| `PERIODICOOL_PORT` | `8383` | Port. Can also be set in Settings, General; this variable wins when set |
| `PERIODICOOL_URL_BASE` | unset | Serve under a path such as `/periodicool` (for a reverse proxy). Also in Settings, General |
| `PERIODICOOL_ALLOWED_HOSTS` | unset | Comma-separated host names to answer to (localhost always works). Also in Settings, General |
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

## Webhooks

Settings, Connect, Add connection, Webhook: PeriodiCool posts events as JSON to an address you choose, like Sonarr's Webhook connection. Point it at Apprise, ntfy, Home Assistant, n8n, a chat bridge or your own script. There are no app-specific integrations; the webhook is the one way out.

**Settings**

- URL, and method POST or PUT.
- Optional username and password, sent as basic authentication.
- Optional extra headers, under Show advanced.

**Events**

- One switch per event. The `eventType` values are `Test`, `Grab`, `Download` (an import, as Sonarr names it), `DownloadFailure`, `ImportFailure`, `ManualInteractionRequired`, `MagazineAdd`, `MagazineDelete`, `Health` and `HealthRestored`.
- Health events come from a check every 5 minutes. Problems are always sent; warnings only when "Include health warnings" is on.

**Sending**

- An event is sent only after the change it reports is saved.
- Redirects are not followed.
- A failed send is logged and shown in the list. It never stops a grab or an import.

**Payload**

- Every event carries `eventType`, `instanceName`, `applicationUrl` (Settings, General, Application URL) and `date` (UTC).
- Depending on the event, it also carries `magazine`, `issue`, `release`, `file` (path relative to the library), `reason` or `health`.
- Keys, passwords, download links and absolute paths are never included.

An example `Grab`:

```json
{
  "eventType": "Grab",
  "instanceName": "PeriodiCool",
  "applicationUrl": "https://mags.example",
  "date": "2026-10-23T18:04:22+00:00",
  "magazine": {"id": 3, "title": "Linux Magazine", "issn": "1471-5678", "frequency": "monthly"},
  "issue": {"key": "n311", "coverDate": "2026-10-01", "issueNumber": 311, "volumeNumber": null, "special": false},
  "release": {"title": "Linux Magazine USA - Issue 311, October 2026", "indexer": "NZBgeek", "size": 52428800,
              "protocol": "usenet", "downloadClient": "SABnzbd", "downloadId": "SABnzbd_nzo_abc123"}
}
```

`python -m periodicool notify test ID` sends a `Test` event from the command line.

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
