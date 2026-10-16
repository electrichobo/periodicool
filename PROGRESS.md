# Progress

Handoff notes between sessions. Read at the start, update at the end.

## Done

- **Step 1 (setup), 2026-10-16:** repo files (README, CONTRIBUTING, CODE_OF_CONDUCT, LICENSE GPL-3.0, CHANGELOG, issue and PR templates), `.gitignore`, skeleton in `src/periodicool`, env config, `/health`, SQLite engine, Alembic with an empty baseline migration, 13 tests, ruff config, CI workflow. Tests and lint pass locally on Python 3.14.9. `/health` smoke-tested against a throwaway config dir.

- **Step 2 (fixtures), 2026-10-16:** `scripts/collect_fixtures.py` (stdlib only, cached, spaced searches) pulled 774 release names from Prowlarr category 7010 into `tests/fixtures/releases/` (9 queries, about 27 indexer API hits). Formats and traps per magazine are summarized in `tests/fixtures/README.md`. Sanity tests check shape and that no links or keys leak in.

- **Step 3 (parser), 2026-10-16:** `periodicool/parser.py` (`parse_release`) and `periodicool/matching.py` (`TitleSpec`, `match_title`). The parser strips post junk (sizes, par2/rar parts, "1 of 7", quoted yEnc subjects, spam tags, HTML entities), then extracts dates (month names incl. German, month pairs, seasons, day-month-year, ISO, numeric), issue/volume/week numbers, double issues, packs, specials, region, language, TruePDF/format/quality/group. Anything ambiguous or left over adds a reason and makes it low confidence. Tests: about 40 hand-checked parses, 13 low-confidence cases, 40 accept/reject match cases, and a snapshot of all 774 fixture names (`tests/fixtures/parser_snapshot.json`, regenerate with `scripts/update_parser_snapshot.py` and review the diff). 547 names match their magazine; 516 of those are high confidence.

## Next

- Step 4: data model and expected-issue generation (plan mode: data model change).

## Parser decisions and known gaps

- Parsing is magazine-agnostic; matching is a separate step with a `TitleSpec` per magazine (title, aliases, region, languages, `filler_optional`, `special_prefix`). These fields will move into the Magazine model in step 4.
- "Magazine"/"Mag" are ignored when comparing titles, except where the spec says they are part of the name (Linux Magazine, so a bare "Linux" does not match).
- Specials may extend the title ("Fortean Times Paranormal Handbook") unless the spec turns that off (People, so "People Wellness Special" is not auto-accepted).
- Region is read from inside the title ("People USA", "Linux Magazine USA"). A spec with a region rejects other regions and accepts names with none.
- Day and month in all-numeric dates are only accepted when one part is over 12; otherwise low confidence ("People USA 02 10 2025").
- Known gap: the Dutch "Linux Magazine #NN (YYYY)" carries no language marker and matches Linux Magazine. Expected-issue checks in step 4 or 6 (issue 1 to 6 in 2026 vs. the real issue 306+) should catch it.
- Known gap: Make uses `No.98` for what is really volume 98. The parser reports what the name says; mapping issue numbers to volumes per numbering scheme belongs in step 4.
- Tabletop Gaming names sometimes carry an on-sale day (`25.November.2022`) on a monthly; step 4 should treat day as optional for monthlies.

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
- Dependencies pinned exactly in `pyproject.toml` (latest as of 2026-10-16).
- Package lives in `src/periodicool`. Migrations ship inside the package (`periodicool/migrations`) so the image can run them on startup.
- The app runs `alembic upgrade head` on startup.
- The baseline migration creates no tables. The settings table and domain tables come in step 4, through plan mode, since they are data model changes.
- Default port 8383 (does not clash with the common *arr ports).
- Env vars are prefixed `PERIODICOOL_` for paths and bind settings; `PUID`, `PGID`, `UMASK`, `TZ` keep their usual names.
- Tag-triggered GHCR image workflow comes with the Dockerfile in step 9. CI for now: ruff and pytest on every push.
- Dev dependency `httpx` swapped for `httpx2` (2026-10-16), which Starlette's TestClient now prefers; the deprecation warning is gone.
- CODE_OF_CONDUCT contact points to the maintainer's GitHub profile, not an email address.

## Open questions

- None.
