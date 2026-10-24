# PeriodiCool

Self-hosted *arr-style manager for magazines: track titles, find new and back issues through Prowlarr, send them to SABnzbd, and keep the library organized. Think Sonarr, where a magazine is the series and an issue is the episode.

- Display name **PeriodiCool**. Slug `periodicool` for the repo, Docker image, container and package names.
- Repo: `github.com/electrichobo/periodicool` (public). Image: `ghcr.io/electrichobo/periodicool`.
- Personal environment details live in `CLAUDE.local.md` (gitignored). Read it too if it exists.

## How to work on this project

- Read `PROGRESS.md` at the start of every session; update it at the end (done, next, open questions). It's the handoff between sessions.
- Commit after each working step with tests passing. Run the test suite before every commit.
- Ask before adding a dependency. Keep dependencies minimal and pinned.
- Use plan mode for anything that changes the data model or moves or deletes files.
- Never commit secrets, `.env`, `CLAUDE.local.md`, real hostnames, IPs, usernames or personal paths. Everything environment-specific is configurable, with generic defaults.

## Public release

- License GPL-3.0 (matches the *arr ecosystem). Include README, CONTRIBUTING, CODE_OF_CONDUCT (Contributor Covenant), issue templates, CHANGELOG, semantic versioning.
- README states that PeriodiCool ships no content and no indexers; users bring their own.
- GitHub Actions: run tests and lint on every push; on version tags build a multi-arch image (amd64 + arm64) and push to GHCR with `latest` and the version tag.
- Self-host fonts and assets inside the image. No CDNs, no telemetry, no outbound calls except to the user's configured services and the opt-in metadata lookups below.

## Scope

**MVP:** track magazines, generate expected issues, find releases (RSS + search), send to SABnzbd, import, organize the library, web UI.

**Non-goals for now:** built-in reader; scraping storefronts (Zinio, Readly, PressReader and the like); any central PeriodiCool server or user-contributed metadata; multi-user roles; cloud services.

**Indexers and download clients are user-entered lists, as in Sonarr and Radarr:** the user adds, names, tests, orders and turns off each one. Prowlarr and SABnzbd are the first kinds; direct Newznab indexers and NZBGet follow. Every indexer and client has a protocol (usenet or torrent).

**Torrents:** Torznab indexers (through Prowlarr or direct) and torrent clients, qBittorrent first. A torrent keeps seeding from the client's folder, so imports hard-link or copy, never move, and PeriodiCool never removes a torrent's files. Seed goals (ratio, time) and minimum seeders are set per indexer entry and passed to the client with each torrent; the client decides what happens when they are met (PeriodiCool never changes its global settings). Torrent indexers are only searched while a torrent client is enabled. No protocol preference: format and quality decide, then indexer priority. The download-client safety rules apply unchanged: own category, only torrents PeriodiCool added. Keep both layers pluggable behind one interface each, and never hard-code a single indexer or client into the rest of the app.

**Opt-in metadata lookups (off by default):** public sources such as Library of Congress MARC records, Wikidata and the Internet Archive may be queried to confirm or fill in a magazine's facts (ISSN, canonical title, aliases, publisher, start year, frequency) and back-issue lists. Rules:
- Each source has its own switch in Settings, off by default. The install notes and every metadata page say clearly that turning them on improves matching.
- Send only what the lookup needs (an ISSN or a title). Cache results in the local database; one lookup per magazine on add or refresh, not ongoing traffic.
- Lookups raise or lower confidence and suggest values; the user confirms before anything changes. Never download content (scans, PDFs) from these sources.

## Safety rules (critical)

The download client and library usually serve other apps and already hold content. Hard rules:

- **Download client:** only add jobs in PeriodiCool's own category. Only read, change or remove jobs PeriodiCool created (tracked by job ID). Never touch other categories, other jobs, or global client settings.
- **Files:** never delete library files. Organizing means moving and renaming only. Never overwrite: on a name collision, keep both and send the newcomer to the review queue. Log every move. Provide a dry-run preview.
- **Indexer limits:** usenet indexers cap daily API hits and grabs. Discover via RSS first; rate-limit searches with backoff; never loop. Searches are always live (no result cache; maintainer's call 2026-10-23), so keep the number of queries per search small. Show remaining limits if Prowlarr exposes them.
- **Permissions:** never run as root in the container. Honor `PUID`/`PGID` (default 99/100 for Unraid) and `UMASK` (default 002).

## Library organization

Everything in the library follows one structure, whether PeriodiCool downloaded it, it was already there, or the user drops it in later.

- Default layout: `{Title}/{Year}/{Title} - {YYYY-MM}.{ext}` (append `-DD` for weeklies). Naming template is configurable.
- **Library scan:** walk the library, parse every file (names first, then a quick read of the PDF's first pages for ISSN, title and date when the name is not enough; never at the cost of scan speed or reliability), match it to a magazine and issue, and propose moves into the standard layout. The first scan shows a full preview before anything moves.
- **Ongoing:** periodic rescan plus an optional drop-in import folder. New files are parsed and moved automatically when confidence is high.
- **Manual import queue** (like Sonarr's): anything unparseable, low-confidence, duplicate or colliding waits for the user to match or skip. Never guess.
- Duplicates of one issue: keep the better format per the format profile; the other goes to the review queue, never deleted.

## Domain model

- **Magazine:** title, aliases, region/edition, frequency (weekly, biweekly, monthly, 13-per-year, bimonthly, quarterly, seasonal, irregular), numbering scheme (date, issue number, volume number, season), monitor mode (new only / from year X / all), format profile, release rules, on-sale grace window.
- **Issue:** magazine, date or season, optional issue/volume number, special flag, status (missing, wanted, grabbed, downloaded, imported, failed), file path.
- Expected issues come from frequency and numbering, so Wanted/Missing works without a metadata provider.

## Release-name parsing (the hard part)

No canonical metadata source exists for magazines; identity comes from release and file names. Handle month names, numeric and ISO dates, seasons, week numbers, issue and volume numbers, regional editions, specials and annuals, True PDF vs scanned, and multi-issue packs. Every parser change ships with tests against real fixtures. Report low-confidence names; never guess.

### Dev test magazines

Chosen to cover the spectrum. Quirks below are expectations; confirm them against fixtures.

| Magazine | Expected quirks |
| - | - |
| American Cinematographer | Monthly, date-named |
| AMAZONAS Magazine | Bimonthly; English and German editions (region filtering) |
| 2600: The Hacker Quarterly | Seasonal issues (Spring/Summer/Autumn/Winter), volume numbers, colon in title |
| Linux Magazine | Monthly, numbered; North American edition is Linux Pro Magazine (aliases) |
| Make: Magazine | Volume-numbered, irregular cadence, colon in title, very generic search term |
| Fortean Times | Numbered issues, roughly 13 per year |
| Tabletop Gaming Magazine | Monthly, UK |
| People | Weekly, very generic one-word title (strict matching), specials, People en Español variant to reject |

If a title returns no usable results, record that in `PROGRESS.md` and move on.

## Release lifecycle (Sonarr-style)

- **Failed downloads:** mark failed, blocklist the release, try the next best candidate automatically.
- **Upgrades:** optional replacement with a better format (True PDF over scanned). Off by default.
- **Cover date vs on-sale date:** issues often appear before their cover date. Use the per-magazine grace window before marking anything missing.
- **Formats:** PDF, EPUB, CBZ, CBR. Reject downloads without one of these.

## Filenames and paths

- `pathlib` everywhere; developed on Windows, runs in a Linux container.
- Sanitize for Linux and SMB: replace `: * ? " < > | /`, strip trailing dots and spaces, keep Unicode. "2600: The Hacker Quarterly" and "Make:" must produce clean folder names.

## UI

- **Look: cassette futurism.** Hardware-panel UI: warm off-white and charcoal panels, amber/orange accents, CRT phosphor readouts for data, LED status lights, chunky buttons with real pressed states, toggle switches, segmented bars and VU-style meters for progress, tape-counter numbers, magazines shown as cassette-label cards, label-maker style tags.
- Monospace or segmented type for data, condensed industrial sans for labels. Self-hosted fonts.
- Effects (scanlines, glow) are subtle, optional, and off under `prefers-reduced-motion`. No flicker.
- Accessibility: WCAG AA contrast, keyboard navigation, status never shown by color alone (LED plus label or icon).
- Build the theme on CSS custom properties so a plain theme can ship later.
- **Functional references:** Sonarr (poster grid, year-by-month issue grid, Wanted, Calendar, Activity, History, release profiles), Radarr (format profiles), Shelfarr (Health page). Interactive search lists every candidate with its accept/reject reason; make that first-class.

## Stack

- Python 3.14 (Docker base image `python:3.14-slim`), FastAPI, SQLite, server-rendered templates with HTMX. Alembic migrations from day one.
- Settings in the database, edited in the UI. Only paths, `PUID`, `PGID`, `UMASK`, `TZ` and the signing secret come from the environment. Indexer and download client API keys are stored with their list entries in the database: write-only in the UI, never logged, included in backups. `PROWLARR_API_KEY` and `SABNZBD_API_KEY` remain a fallback for the first Prowlarr and SABnzbd entry.
- Container paths: `/config` (DB, settings), `/library`, `/downloads` (the download client's completed folder for PeriodiCool's category, mounted at the same path the client uses where possible; support remote path mapping otherwise), optional `/import` drop folder. `/health` endpoint.
- Background jobs: RSS sync every 15 minutes, download monitor by job ID, scheduled Wanted searches with backoff, periodic library rescan.
- Login on by default (one admin, hashed password, CSRF on forms); option to disable for LAN-only use. REST API under `/api/v1` with an API key. Decision log viewable in the UI; never log secrets. Optional generic webhook notifications (Sonarr's Webhook connection: JSON events to a user-entered URL); no app-specific integrations such as Discord.

## Build order

1. Setup: repo files, `.gitignore` (incl. `.env`, `CLAUDE.local.md`), `PROGRESS.md`, skeleton, migrations, tests, CI.
2. Fixtures: release titles for the dev magazines from Prowlarr category 7010, saved to `tests/fixtures/`.
3. Parser and tests.
4. Data model and expected-issue generation.
5. Library scan, organize with preview, manual import queue.
6. Prowlarr search and RSS sync with reasons and rate limiting.
7. SABnzbd send, monitor, import, failed-download handling.
8. Web UI (cassette futurism) and login.
9. Dockerfile, GHCR multi-arch workflow, example compose.
10. API, notifications, docs, Unraid Community Apps template.

## Writing style (code comments, UI copy, docs, commits)

- No em dashes. Use commas, periods, parentheses, colons or semicolons.
- Plain words. Avoid filler like delve, tapestry, realm, robust, seamless, leverage, elevate.
