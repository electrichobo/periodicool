# Progress

Handoff notes between sessions. Read at the start, update at the end.

## Done

- **Step 1 (setup), 2026-10-21:** repo files (README, CONTRIBUTING, CODE_OF_CONDUCT, LICENSE GPL-3.0, CHANGELOG, issue and PR templates), `.gitignore`, skeleton in `src/periodicool`, env config, `/health`, SQLite engine, Alembic with an empty baseline migration, 13 tests, ruff config, CI workflow. Tests and lint pass locally on Python 3.14.9. `/health` smoke-tested against a throwaway config dir.

- **Step 2 (fixtures), 2026-10-21:** `scripts/collect_fixtures.py` (stdlib only, cached, spaced searches) pulled 774 release names from Prowlarr category 7010 into `tests/fixtures/releases/` (9 queries, about 27 indexer API hits). Formats and traps per magazine are summarized in `tests/fixtures/README.md`. Sanity tests check shape and that no links or keys leak in.

- **Step 3 (parser), 2026-10-21:** `periodicool/parser.py` (`parse_release`) and `periodicool/matching.py` (`TitleSpec`, `match_title`). The parser strips post junk (sizes, par2/rar parts, "1 of 7", quoted yEnc subjects, spam tags, HTML entities), then extracts dates (month names incl. German, month pairs, seasons, day-month-year, ISO, numeric), issue/volume/week numbers, double issues, packs, specials, region, language, TruePDF/format/quality/group. Anything ambiguous or left over adds a reason and makes it low confidence. Tests: about 40 hand-checked parses, 13 low-confidence cases, 40 accept/reject match cases, and a snapshot of all 774 fixture names (`tests/fixtures/parser_snapshot.json`, regenerate with `scripts/update_parser_snapshot.py` and review the diff). 547 names match their magazine; 516 of those are high confidence.

- **Step 4 (data model), 2026-10-21:** migration `0002` adds `settings`, `format_profiles` (seeded "Any": pdf, epub, cbz, cbr, prefer TruePDF, upgrades off), `magazines` and `issues` (`src/periodicool/models.py`). `src/periodicool/schedule.py`: expected-issue generation per frequency from an anchor issue, generation range by monitor mode bounded by `schedule_start`, `sync_expected_issues` (insert-only, idempotent), computed "missing" status, and `identify` (parsed release to issue key with schedule checks). Dev magazine configs in `tests/dev_magazines.py` are now `Magazine` objects. 947 tests pass; fresh-DB smoke test OK.

- **Step 5 (library), 2026-10-21:** migration `0003` (`scans`, `import_items` = plan and manual import queue, `file_moves` = move log; `issues.cover_date` nullable). `naming.py` (SMB-safe `sanitize`, template `render`, root guard), `library.py` (`plan_scan` preview, `apply_scan`, `queue`/`skip`/`resolve`, `process_import_folder`), CLI in `cli.py` with `examples/magazines.json`. 993 tests pass. Manual run on a scratch library: 5 moved, 3 queued, cover.jpg untouched.

- **Step 6 (search), 2026-10-21:** migration `0004` (`releases`, `search_cache`, `search_state`, `api_calls`, `jobs`, `magazines.search_term`). `prowlarr.py` (stdlib client: search, Newznab RSS feed, indexers, status, stats; request spacing; API key redacted everywhere and stripped from stored URLs), `decisions.py` (`evaluate` with every reason, ranking, `best_candidates`), `search.py` (`rss_sync`, `search_magazine` with cache, per-magazine backoff, `wanted_search`, `indexer_limits`), `jobs.py` (asyncio scheduler in the app: rss 15 min, wanted 6 h, library 24 h, import 5 min; `PERIODICOOL_JOBS=off`). CLI: `config`, `indexers`, `search`, `rss`, `wanted`, `jobs`. `tzdata==2026.5` added. 1033 tests pass. Live, read-only: indexers listed, RSS feed endpoint `/{id}/api` confirmed (3 feeds, 200 releases, no API key stored), one Fortean Times search (144 candidates, all rejected with correct reasons). About 6 indexer hits in total.
- **Migration safety fix:** SQLite batch migrations rebuild tables; with foreign keys on, dropping the old `magazines` table cascade-deleted `issues` (caught by the downgrade test). Migrations now run with foreign keys off and finish with `PRAGMA foreign_key_check`.

## Next

- **Before any real library run:** do a preview on the server against the real library (`scan` only, read the output, apply nothing) and review the queue counts. The LazyLibrarian files are the first real test of the parser on file names.
- Step 7: SABnzbd send (PeriodiCool's category only, jobs tracked by ID), download monitor, import, failed-download handling and blocklist. `best_candidates()` gives the release to grab per issue; the download URL needs the API key added back at grab time.

## Library decisions (step 5)

- Planning never touches files; `apply` is always a separate command. The import folder auto-applies only confident moves, only when `import.auto_apply` is on and after the first library scan has been applied.
- Moves are a hard link to the new name plus removal of the old name (atomic, fails if the target exists). Where links are unsupported: exists check plus rename. Cross-device copies are only allowed for import-folder files (exclusive-create temp file, size check, then the source is removed). Library files never cross devices; the item fails instead.
- A target that exists, or appears between plan and apply, sends the newcomer to review; both files stay.
- Duplicates of one issue: profile format order, then TruePDF if preferred; the loser goes to review. Equal candidates both go to review. An issue that already has a file is never replaced (upgrades are a later feature).
- Thirteen-per-year: two files with different known numbers in one month get keys `YYYY-MM` and `YYYY-MM-2`.
- Names are tried as the file stem, then with the parent and grandparent folder names in front (`Fortean Times/2023/2023-11.pdf`).
- Files of a tracked title rejected only for region or language say so in the queue ("People: region Australia, wanted USA").
- Empty folders left behind after moves are not removed (that would be a delete); a cleanup option can come later.
- Not in the approved plan: `apply --confident-only` was dropped, since only confident files are ever planned as moves.

## Parser decisions and known gaps

- Parsing is magazine-agnostic; matching is a separate step with a `TitleSpec` per magazine (title, aliases, region, languages, `filler_optional`, `special_prefix`). These fields will move into the Magazine model in step 4.
- "Magazine"/"Mag" are ignored when comparing titles, except where the spec says they are part of the name (Linux Magazine, so a bare "Linux" does not match).
- Specials may extend the title ("Fortean Times Paranormal Handbook") unless the spec turns that off (People, so "People Wellness Special" is not auto-accepted).
- Region is read from inside the title ("People USA", "Linux Magazine USA"). A spec with a region rejects other regions and accepts names with none.
- Day and month in all-numeric dates are only accepted when one part is over 12; otherwise low confidence ("People USA 02 10 2025").
- The Dutch "Linux Magazine #NN (YYYY)" still matches by title, but `identify` flags it: issue 4 does not fit 2024 (expected 278 to 289).
- Make `No.98` is read as volume 98 by `identify` for volume-numbered magazines.
- A day on a monthly (`Tabletop.Gaming-25.November.2022`) is kept as a note, not identity.

## Schedule decisions (step 4)

- Issue identity is `issues.key`, unique per magazine: `YYYY-MM` (monthly, bimonthly, quarterly, 13-per-year), `YYYY-MM-DD` (weekly), `YYYY-season`, `nNNN` (issue-numbered), `vNN` (volume-numbered), `special-YYYY-MM[-DD]`.
- Numbers step from one anchor issue (`anchor_date` + `anchor_number`) in both directions. Nothing is generated before `schedule_start` (Tabletop Gaming: 2018, because it was seasonal and bimonthly before that).
- Thirteen-per-year (Fortean Times) is keyed by cover month, generated monthly without numbers; numbers are stored when a name has one and checked loosely (13 a year from the anchor, within 2). Names with a number but no month go to review. Chosen with the maintainer over a 28-day cadence, which left 87 of 107 names unidentifiable.
- Seasonal cover months are Mar/Jun/Sep/Dec (inferred); the season is the identity.
- Weeklies: a name date up to 3 days from the schedule day snaps to it (on-sale vs. cover date); further away is flagged.
- Monthly numbered magazines can take a number from the schedule for a date-only name; 13-per-year cannot.
- "missing" is computed (`effective_status`): monitored, wanted, no file, past `cover_date + grace_days`. Stored statuses are wanted, grabbed, downloaded, imported, failed.
- Monitor modes: new_only = current and next issue; from_year = Jan 1 of that year; all = from `schedule_start`. Always up to the next issue after today. Generation is capped at 3000 issues per call.

## Fixture findings vs. CLAUDE.md expectations

- **AMAZONAS:** only 12 results, all 2013 and 2017, and none name a language. The English/German edition split is unconfirmed; region filtering can't be tested from these fixtures.
- **2600:** seasonal names confirmed. No volume numbers appear in release names.
- **Linux Magazine:** numbered confirmed (`No.NNN`), plus date-named "Linux Magazine USA" releases. The "Linux Pro Magazine" query returned nothing for the English title, only the unrelated Italian "Linux Pro". The North American edition shows up as "Linux Magazine USA", so that is the alias to use. Dutch "Linux Magazine #NN" and German "Linux-Magazin" are different magazines to reject.
- **Make:** volume numbers confirmed, plus `Issue.NN`, `No.NN` and date names; multi-issue packs present. Generic query pulls in WineMaker, Musicmaker and others.
- **Fortean Times:** issue numbers confirmed (351 in March 2017, 437 in November 2023, about 13 a year).
- **Tabletop Gaming:** monthly now, but early issues were seasonal (2015 to 2016) and bimonthly (2017). Frequency changes over time.
- **People:** weekly, specials, regional editions (USA, Australia, South Africa) and "People.en.Espaol" (ñ dropped) all present, plus many false positives.
- Recent coverage is thin for some titles (American Cinematographer newest is 2024-02, AMAZONAS 2017). Prowlarr returns up to 100 results per indexer per query.

## Decisions

- Python pinned to 3.14 (`requires-python = ">=3.14,<3.15"`, CI on 3.14). Docker base image will be `python:3.14-slim` (step 9).
- Dependencies pinned exactly in `pyproject.toml` (latest as of 2026-10-21).
- Package lives in `src/periodicool`. Migrations ship inside the package (`periodicool/migrations`) so the image can run them on startup.
- The app runs `alembic upgrade head` on startup.
- The baseline migration creates no tables. The settings table and domain tables come in step 4, through plan mode, since they are data model changes.
- Default port 8383 (does not clash with the common *arr ports).
- Env vars are prefixed `PERIODICOOL_` for paths and bind settings; `PUID`, `PGID`, `UMASK`, `TZ` keep their usual names.
- Tag-triggered GHCR image workflow comes with the Dockerfile in step 9. CI for now: ruff and pytest on every push.
- Dev dependency `httpx` swapped for `httpx2` (2026-10-21), which Starlette's TestClient now prefers; the deprecation warning is gone.
- CODE_OF_CONDUCT contact points to the maintainer's GitHub profile, not an email address.

## Search decisions (step 6)

- No universal search cap (maintainer decision: match Sonarr/Radarr). Per-indexer query and grab limits live in Prowlarr; PeriodiCool reads them with today's usage, leaves out indexers at their limit or disabled by Prowlarr, and backs off on 429/503 (5 min doubling to 6 h). `search.daily_cap` exists but is unset.
- One search per magazine (search term, then each alias) covers all its issues. Results are cached 6 h. Scheduled searches back off per magazine: 6 h doubling to 7 days, reset by a new accepted release. At most 5 magazines per Wanted run, each once.
- `evaluate` keeps going after the first rejection so interactive search shows every reason.
- Thirteen-per-year number-only names (`Fortean.Times.No.475.2026`): during release evaluation the month is estimated at 13 a year from the newest known issue with both a number and a month (or the anchor). Accepted only if the number is newer than every known issue number, the estimate falls in the year the name gives, and the month does not already hold a different issue. The estimate is stored as a `note:` entry in `releases.reasons` and shown in search output. Library organizing never estimates; such files go to the queue. Live: No.475 is accepted as 2026-10, No.474 (2026-09) is outside the new-only range.
- Indexer usage counts RSS plus searches (errs on the safe side). Prowlarr's `indexerstats` returns nothing when start and end fall on one day, so only `startDate` is sent and Prowlarr counts up to its own clock.

## Notes for later steps

- **Clock check for Health (step 8):** the dev machine's clock once ran 5 days slow because Windows Time had never synced; dates, grace windows and "today" all depend on it. The Health page should compare the local clock with the `Date` header Prowlarr or SABnzbd sends and warn above a few minutes. Git commits from before 2026-10-21 carry timestamps 5 days early.

## Open questions


- PDF text reading for unidentifiable files is deferred (needs a dependency such as pypdf). Revisit after the first real preview shows how many files land in the queue.
- Time zone for "today": background jobs (step 6) need it. On Windows `zoneinfo` needs the `tzdata` package; adding it needs approval. Step 4 functions take `today` as an argument.
