# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Project skeleton: FastAPI app, `/health` endpoint, SQLite with Alembic migrations, tests, lint and CI.
- Release-name fixtures for the eight dev magazines, collected from Prowlarr category 7010 by `scripts/collect_fixtures.py`.
- Release-name parser (`parse_release`) with confidence reasons, and strict title matching with region and language filters. Snapshot test over every fixture name.
