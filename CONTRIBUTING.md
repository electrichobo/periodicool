# Contributing to PeriodiCool

Thanks for helping. A few ground rules keep the project safe for people's existing libraries and download clients.

## Before you start

- Open an issue first for anything bigger than a small fix, so we can agree on the approach.
- Read the safety rules below. Pull requests that break them will not be merged.

## Safety rules

- **Download client:** only add jobs in PeriodiCool's own category. Only read, change or remove jobs PeriodiCool created. Never touch other categories, other jobs or global client settings.
- **Files:** never delete library files. Organizing means moving and renaming only. Never overwrite; on a collision, keep both and send the newcomer to the review queue.
- **Indexers:** respect API limits. Prefer RSS, rate-limit searches, cache results, never loop.
- **Privacy:** no telemetry, no CDNs, no outbound calls except to services the user configured.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -c requirements.lock -e ".[dev]"
pytest
ruff check .
ruff format --check .
```

## Dependencies

Runtime dependencies are pinned in `pyproject.toml`, and every runtime package (including indirect ones) is pinned in `requirements.lock`, which CI and the image use as constraints. After changing a dependency, regenerate the lock file as described at its top.

## Pull requests

- One change per pull request, with tests.
- Parser changes must include test cases built from real release names in `tests/fixtures/`.
- Run `pytest`, `ruff check .` and `ruff format --check .` before pushing. CI runs the same checks.
- Add a line under `[Unreleased]` in `CHANGELOG.md`.
- Use `pathlib` for paths. Code runs on Linux in a container but is also developed on Windows.

## Writing style

Code comments, UI text, docs and commit messages: plain words, no em dashes.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
