# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

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

[Unreleased]: https://github.com/electrichobo/periodicool/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/electrichobo/periodicool/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/electrichobo/periodicool/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/electrichobo/periodicool/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/electrichobo/periodicool/releases/tag/v0.1.0
