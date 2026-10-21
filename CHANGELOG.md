# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Project skeleton: FastAPI app, `/health` endpoint, SQLite with Alembic migrations, tests, lint and CI.
- Release-name fixtures for the eight dev magazines, collected from Prowlarr category 7010 by `scripts/collect_fixtures.py`.
- Release-name parser (`parse_release`) with confidence reasons, and strict title matching with region and language filters. Snapshot test over every fixture name.
- Data model (settings, format profiles, magazines, issues) with migration 0002, expected-issue generation per frequency, and release-to-issue identification with schedule checks.
- Library scan with a dry-run preview, organize (move and rename only, never overwrite, every move logged), manual import queue, drop-in import folder, and a command line for magazines, scans and the queue.
- Prowlarr RSS sync and magazine search with an accept/reject reason for every candidate, per-indexer limits read from Prowlarr, request spacing, result cache, per-magazine backoff, and background jobs (RSS, Wanted search, library rescan, import folder).
- Thirteen-per-year magazines: release names with an issue number but no month get an estimated month when the issue is newer than any known one; the estimate is flagged.
- SABnzbd support: grab (NZB uploaded into PeriodiCool's category only), download monitor by job ID, import into the library, failed-download handling with blocklist and automatic retry of the next best release, and removal of the job's leftover folder after a verified import. Auto-grab is off by default.
- Docker image (python:3.14-slim) that runs as PUID:PGID and never as root, with a health check; multi-arch (amd64, arm64) release workflow to GHCR on version tags; example compose file; `requirements.lock` pinning every runtime package.

### Changed

- Dev dependency `httpx` replaced by `httpx2` for FastAPI's TestClient.
- Added `tzdata` so the `TZ` setting works on every platform.

### Fixed

- Migrations run with SQLite foreign keys off, so rebuilding a table can no longer cascade-delete rows in other tables.
