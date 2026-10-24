# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Indexer forms the Sonarr way:
  - Enable RSS, Enable Automatic Search and Enable Interactive Search replace the single Enabled switch. Existing entries get all three set as they were.
  - Each kind has its own fields, with advanced ones (API path, additional parameters, daily limits, torrent options) behind Show advanced.
  - Load categories ticks the categories the indexer reports.
  - An indexer can have its own download client.
- Sonarr's usenet indexer presets in the Add picker. They fill in the address only.
- Reject blocklisted torrent hashes, per indexer: torrent failures now record their info hash on the blocklist.

### Fixed

- Newznab capabilities now include subcategories, where indexers list 7010 Magazines.
- Grabbing a torrent that was already sent once (same hash, another name) failed with a database error; it is now refused with a reason.

## [0.1.7] - 2026-10-23

### Added

- Download clients the Sonarr way. Add lists every supported client, grouped under Usenet and Torrents, each with a More info note. New: NZBGet, Transmission, Deluge (Label plugin) and rTorrent (also through ruTorrent).
- Each client's form has that client's fields with its usual defaults: host, port, SSL, URL base, credentials, category or label, recent and older priority, add paused or initial state, and where it applies a directory, a post-import category and remove completed.
- Recent or older priority is picked by the issue's cover date (14 days, a month-only date counting from the month's end).
- After an import, a torrent can move to a post-import category, and a finished usenet job can leave the client's history. Only PeriodiCool's own job, never its files.

### Changed

- Existing download client entries keep working: their address is split into host, port, SSL and URL base. `client add` on the command line does the same and stores a password client's secret as its password.

## [0.1.6] - 2026-10-22

### Changed

- Add magazine is a search now: type a title or ISSN, pick one of the likely matches, and confirm a short form (title, frequency, numbering, what to monitor, format profile); everything else waits under More options. Matches come from your indexers (release names grouped into magazines, with frequency, numbering and the latest issue guessed from them; one cached search) and, when you turned them on, the Library of Congress and Wikidata (magazines and journals only). What the catalogs said about the match you pick becomes the magazine's metadata, so its Metadata pop-up does not ask again.

### Added

- A Monitor button on each magazine: New issues only, From a year, or All issues. Saving adds the back issues that brings in, and with the Internet Archive lookup on it offers the archive's past issues as well.

- Torrents, alongside usenet:
  - qBittorrent as a download client (username and write-only password; both may be empty when qBittorrent skips login for PeriodiCool).
  - Torznab indexers added directly, and the torrent indexers in Prowlarr. Torrent indexers are only searched while a torrent client is enabled, so a usenet-only setup spends no API hits on them.
  - Seed ratio, seed time and minimum seeders per indexer entry. The seed goals go to qBittorrent with each torrent, and qBittorrent decides what happens when they are met.
  - Interactive search shows torrents with their seeders and peers.
- A torrent is imported by hard link (or a copy when the library is on another disk), never moved, so it keeps seeding. PeriodiCool never deletes a torrent's files: Remove takes the torrent out of qBittorrent only, and leftover cleanup skips torrents.
- PeriodiCool never reads or changes qBittorrent's settings, and only ever sees torrents it added, in its own category.

## [0.1.5] - 2026-10-22

### Changed

- New look from the maintainer's mockups: a light deck with a dark header bar (and a dark mode that follows the device or Settings, UI), Chelsea Market for headings and labels, lamp buttons drawn from the maintainer's artwork, green lamp switches, and a Sys nominal / warning / fault light in the header. Every text color pair meets WCAG AA in both modes.
- Library is no longer in the menu. Its scan preview and manual import queue are the Manual import page, opened from the Magazines toolbar (with Scan for changes) or Settings, Media management.
- Profiles is part of Settings, Media management; Health is part of System, Status, which also has a More info panel.
- Issue rows on a magazine page and on Wanted have an automatic search (magnifying glass) and an interactive search (person) button; click a row for its details. A year jump sits above the issue list.
- Remote path mappings are their own list on Settings, Download clients, with the client for each mapping.
- Backup interval and count moved from Settings, General to System, Backups.

### Added

- Indexers: maximum size, minimum age and usenet retention for every release (0 for no limit), each with a reason when a release is rejected.
- A folder browser for path fields (remote path mappings, backup and log folders).
- Settings, General: port, URL base (for a reverse proxy) and allowed hosts, applied after a restart. `PERIODICOOL_PORT`, `PERIODICOOL_URL_BASE` and `PERIODICOOL_ALLOWED_HOSTS` win when set.
- Settings, UI: week column header, short and long date formats, 12 or 24 hour time, relative dates, and Light / Dark / Auto.
- System, Backups: delete a backup, and choose the backup folder. System, Log files: delete old log files, and choose the log folder.
- Magazine cards show the cover of the newest issue, taken from page 1 of its PDF when the magazine has no cover image of its own (cached in /config/covers).
- Wanted: filter by name, magazine and status.

## [0.1.4] - 2026-10-21

### Added

- Indexer and download client lists, as in Sonarr and Radarr. Settings, Indexers and Settings, Download clients list every entry with its status; Add opens a pop-up to pick the kind, then name, address, API key, priority and on/off, with a Test button. Each entry can be tested, edited and deleted.
- Direct Newznab indexers, alongside Prowlarr, with their own daily API and download limits.
- Command line: `indexer list|add|remove` and `client list|add|remove`. Keys are asked for or read from a named environment variable, never typed on the command line.

### Changed

- An existing Prowlarr and SABnzbd setup becomes the first entry of each list on upgrade; nothing has to be entered again.
- API keys are stored in the database with their entry: never shown again, scrubbed from the logs, and included in backups (the Backups page says so). `PROWLARR_API_KEY` and `SABNZBD_API_KEY` still work for the first Prowlarr and SABnzbd entry.
- Searches and RSS run on every enabled indexer. Backoff and the search cache are per indexer, so one indexer at its limit no longer stops the others. Indexer priority breaks ties between equally good releases.
- A grab goes to the highest-priority enabled download client and remembers it; monitoring, Remove and Retry import use that client and its remote path mappings.
- Health checks each indexer and download client.
- When a finished download cannot be found, the review message explains how to mount the download client's folder (or set a remote path mapping) and to press Retry import, instead of naming an internal setting.

## [0.1.3] - 2026-10-21

### Added

- Retry import: a finished download waiting in review (for example after a folder permission problem) can be imported again from Activity, Queue once the cause is fixed.

### Fixed

- Commands run with `docker exec` now switch to `PUID`:`PGID` first. They used to run as root, so a library apply created folders the server could not write into afterwards.

### Changed

- The example compose file mounts the download client's folder exactly as the client sees it (`/data`), like Sonarr and Radarr.

## [0.1.2] - 2026-10-21

### Fixed

- A download that fails because of a problem on the download client's side (a folder it cannot create, permissions, a full disk) is no longer blocklisted or retried. It is marked failed with a hint, and the issue is wanted again so the same release can be grabbed once the cause is fixed.
- Health now warns when the downloads folder is not writable, and says when it looks like a folder Docker created as root.
- An import whose download folder belongs to another user no longer fails after the file is already in the library: the original and the leftovers stay in the download folder, with a warning in the log.

## [0.1.1] - 2026-10-21

### Added

- System, Console: a live view of the log with a level filter, pause, and a Verbose switch that adds debug lines for one hour.

### Fixed

- NZB downloads from indexers that Prowlarr redirects to (indexers set to "Redirect") failed behind Cloudflare. PeriodiCool now sends its own User-Agent, never follows redirects automatically (so API keys never travel to another host), follows one redirect for NZB downloads without any keys, and shows such errors as a readable sentence.

## [0.1.0] - 2026-10-21

First release.

### Added

- Project skeleton: FastAPI app, `/health` endpoint, SQLite with Alembic migrations, tests, lint and CI.
- Release-name fixtures for the eight dev magazines, collected from Prowlarr category 7010 by `scripts/collect_fixtures.py`.
- Release-name parser (`parse_release`) with confidence reasons, and strict title matching with region and language filters. Snapshot test over every fixture name.
- Data model (settings, format profiles, magazines, issues) with migration 0002, expected-issue generation per frequency, and release-to-issue identification with schedule checks.
- Library scan with a dry-run preview, organize (move and rename only, never overwrite, every move logged), manual import queue, drop-in import folder, and a command line for magazines, scans and the queue.
- Prowlarr RSS sync and magazine search with an accept/reject reason for every candidate, per-indexer limits read from Prowlarr, request spacing, result cache, per-magazine backoff, and background jobs (RSS, Wanted search, library rescan, import folder).
- Thirteen-per-year magazines: release names with an issue number but no month get an estimated month when the issue is newer than any known one; the estimate is flagged.
- SABnzbd support: grab (NZB uploaded into PeriodiCool's category only), download monitor by job ID, import into the library, failed-download handling with blocklist and automatic retry of the next best release, and removal of the job's leftover folder after a verified import. Auto-grab is off by default.
- Web UI groundwork: cassette-futurism theme at an 80% scale, vendored fonts and htmx, and shared parts (pop-up, toasts, confirm dialog, page toolbar, sortable and filterable tables with a column chooser and bulk select, tabs, collapsible years, pager, charts), shown at `/ui-kit`.
- Login: first-run setup of one admin (scrypt password hash), 30-day sliding sessions stored as token hashes, CSRF tokens on every form and htmx request, backoff after failed logins, `auth.mode` (`required`, `local`, `off`), an API key in `/config/api.key`, and `user reset-password` on the command line.
- History of grabs, failures, imports and file moves; an event log table and a rotating log file in `/config/logs`, both with secrets redacted; file size, format and import date on issues; magazine tags and sidecar cover images; download size and time left; removing PeriodiCool's own jobs from SABnzbd (never other jobs).
- Magazine screens: index with cards, table and overview views, filters and totals; add form with a live preview; detail page with years, issue rows, a chart and pop-ups for interactive search, issue details and scan-and-rename; mass editor. Deleting a magazine removes database rows only.
- Activity (queue with progress and time left, history with details and mark-as-failed, blocklist), Wanted (missing, cutoff unmet), and a calendar with month, week and agenda views plus an iCal feed protected by the API key. Times show in the configured time zone.
- Settings: media management with a naming template preview, format profiles, indexers with a Prowlarr test and limit meters, download client with an SABnzbd test and remote path mappings, tags, general (login mode, API key, logs, backups) and UI. Global release rules for all magazines.
- System pages: status, health checks with a clock check against Prowlarr and SABnzbd, scheduled tasks with Run now, backups (scheduled, download, restore with a safety copy), events and log files. Library page with a full scan preview and the manual import queue.
- ISSN and an optional on-sale lead time on each magazine. When a file name is not enough, the library scan reads the PDF's first pages (pypdf) for an ISSN, cover date and issue number; download import uses the ISSN to catch wrong editions.
- Opt-in metadata lookups (Library of Congress, Wikidata, Internet Archive), all off by default: a Metadata pop-up per magazine to compare and apply other names, ISSN and frequency, past-issue lists from the Internet Archive, suggestions on the add form, a monthly cache refresh, and a health check for frequency mismatches. Only an ISSN or title is sent; answers are cached locally.
- Docker image (python:3.14-slim) that runs as PUID:PGID and never as root, with a health check; multi-arch (amd64, arm64) release workflow to GHCR on version tags; example compose file; `requirements.lock` pinning every runtime package.

### Changed

- Added `pypdf` 6.19.0 for reading PDF text.
- Dev dependency `httpx` replaced by `httpx2` for FastAPI's TestClient.
- Added `tzdata` so the `TZ` setting works on every platform.

### Fixed

- File names PeriodiCool writes are read back correctly: tracked titles are recognized first, so "2600 - The Hacker Quarterly - 2026-Autumn" no longer loses its leading number, and year-first season labels parse. Agreeing clues in a name (an issue number that fits the month) are noted and preferred when ranking releases.
- Migrations run with SQLite foreign keys off, so rebuilding a table can no longer cascade-delete rows in other tables.

[Unreleased]: https://github.com/electrichobo/periodicool/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/electrichobo/periodicool/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/electrichobo/periodicool/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/electrichobo/periodicool/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/electrichobo/periodicool/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/electrichobo/periodicool/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/electrichobo/periodicool/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/electrichobo/periodicool/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/electrichobo/periodicool/releases/tag/v0.1.0
