# Progress

Handoff notes between sessions. Read at the start, update at the end.

## Done

- **Step 1 (setup), 2026-10-16:** repo files (README, CONTRIBUTING, CODE_OF_CONDUCT, LICENSE GPL-3.0, CHANGELOG, issue and PR templates), `.gitignore`, skeleton in `src/periodicool`, env config, `/health`, SQLite engine, Alembic with an empty baseline migration, 13 tests, ruff config, CI workflow. Tests and lint pass locally on Python 3.14.9. `/health` smoke-tested against a throwaway config dir.

- **Step 2 (fixtures), 2026-10-16:** `scripts/collect_fixtures.py` (stdlib only, cached, spaced searches) pulled 774 release names from Prowlarr category 7010 into `tests/fixtures/releases/` (9 queries, about 27 indexer API hits). Formats and traps per magazine are summarized in `tests/fixtures/README.md`. Sanity tests check shape and that no links or keys leak in.

## Next

- Step 3: parser and tests against the fixtures. Expected outputs per fixture title need to be written by hand (or reviewed by the maintainer), since there is no ground truth.

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
- CODE_OF_CONDUCT contact points to the maintainer's GitHub profile, not an email address.

## Open questions

- Starlette warns that `httpx` with its TestClient is deprecated in favor of `httpx2`. Harmless for now; swapping the dev dependency needs approval.
