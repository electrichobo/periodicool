"""Command line: run the server, manage magazines, scan and organize the library.

python -m periodicool                         run the web server
python -m periodicool magazines import FILE   add magazines from a JSON file
python -m periodicool magazines list
python -m periodicool scan [ROOT] [--import]  plan moves (preview only)
python -m periodicool apply SCAN_ID           carry out a planned scan
python -m periodicool queue                   files waiting for review
python -m periodicool skip ITEM_ID
python -m periodicool resolve ITEM_ID MAGAZINE_ID KEY
python -m periodicool config get|set KEY [VALUE]   e.g. config set prowlarr.url http://...
python -m periodicool indexers                indexer limits and what is left today
python -m periodicool search MAGAZINE_ID      every candidate with accept/reject reasons
python -m periodicool rss                     one RSS sync now
python -m periodicool wanted                  wanted and missing issues
python -m periodicool jobs                    background job status
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from periodicool import db, library, search
from periodicool.config import Settings, load_settings
from periodicool.jobs import today_in
from periodicool.models import (
    FormatProfile,
    Frequency,
    ImportItem,
    ItemAction,
    Job,
    Magazine,
    MonitorMode,
    Numbering,
    ScanKind,
    SearchState,
)
from periodicool.prowlarr import ProwlarrError
from periodicool.schedule import effective_status, sync_expected_issues

_DATE_FIELDS = ("anchor_date", "schedule_start")
_ENUM_FIELDS = {"frequency": Frequency, "numbering": Numbering, "monitor_mode": MonitorMode}
_ALLOWED = {
    "title",
    "aliases",
    "region",
    "languages",
    "filler_optional",
    "special_prefix",
    "frequency",
    "numbering",
    "anchor_date",
    "anchor_number",
    "schedule_start",
    "publish_weekday",
    "grace_days",
    "monitored",
    "monitor_mode",
    "monitor_from_year",
    "must_contain",
    "must_not_contain",
    "search_term",
}
_SECRET_WORDS = ("key", "password", "token", "secret")
# Tests replace this with a fake transport; None means real HTTP.
TRANSPORT = None


@contextmanager
def _session(settings: Settings):
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    try:
        with db.make_session_factory(engine)() as session:
            yield session
            session.commit()
    finally:
        engine.dispose()


def magazine_from_dict(data: dict) -> Magazine:
    unknown = set(data) - _ALLOWED
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    values = dict(data)
    for name in _DATE_FIELDS:
        if values.get(name):
            values[name] = date.fromisoformat(values[name])
    for name, enum in _ENUM_FIELDS.items():
        if name in values:
            values[name] = enum(values[name])
    return Magazine(**values)


def cmd_magazines_import(session, settings, args, out) -> int:
    entries = json.loads(Path(args.file).read_text(encoding="utf-8"))
    profile = session.scalars(select(FormatProfile).order_by(FormatProfile.id)).first()
    existing = {m.title for m in session.scalars(select(Magazine))}
    for entry in entries:
        if entry["title"] in existing:
            print(f"exists, unchanged: {entry['title']}", file=out)
            continue
        mag = magazine_from_dict(entry)
        mag.format_profile = profile
        session.add(mag)
        print(f"added: {mag.title}", file=out)
    return 0


def cmd_magazines_list(session, settings, args, out) -> int:
    for m in session.scalars(select(Magazine).order_by(Magazine.id)):
        print(f"{m.id:4}  {m.title}  ({m.frequency.value}, {m.numbering.value})", file=out)
    return 0


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    return p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)


def cmd_scan(session, settings, args, out) -> int:
    kind = ScanKind.IMPORT if args.import_folder else ScanKind.LIBRARY
    default = settings.import_dir if kind is ScanKind.IMPORT else settings.library_dir
    root = Path(args.root or default or "").resolve()
    if not root.is_dir():
        print(f"not a folder: {root}", file=out)
        return 2
    scan = library.plan_scan(session, root, kind, settings.library_dir)
    items = session.scalars(
        select(ImportItem).where(ImportItem.scan_id == scan.id).order_by(ImportItem.source_path)
    ).all()
    print(f"Scan {scan.id} of {root}", file=out)
    print("  " + ", ".join(f"{k} {v}" for k, v in scan.counts.items()), file=out)
    for item in items:
        src = _rel(item.source_path, root)
        if item.action is ItemAction.MOVE:
            print(f"MOVE    {src}\n     -> {item.target_path}", file=out)
        elif item.action is ItemAction.KEEP:
            print(f"KEEP    {item.target_path}", file=out)
        else:
            print(f"REVIEW  {src}  [{item.reason_code.value}] {item.reason}", file=out)
    print(
        f"Preview only. Nothing was moved. To apply: python -m periodicool apply {scan.id}",
        file=out,
    )
    return 0


def cmd_apply(session, settings, args, out) -> int:
    result = library.apply_scan(session, args.scan_id, settings.library_dir)
    print(", ".join(f"{k} {v}" for k, v in result.items()), file=out)
    return 0 if not result["failed"] else 1


def cmd_queue(session, settings, args, out) -> int:
    for item in library.queue(session):
        print(
            f"{item.id:5}  [{item.reason_code.value}] {item.source_path}\n       {item.reason}",
            file=out,
        )
    return 0


def cmd_skip(session, settings, args, out) -> int:
    item = library.skip(session, args.item_id)
    print(f"skipped {item.source_path}", file=out)
    return 0


def cmd_resolve(session, settings, args, out) -> int:
    try:
        item = library.resolve(
            session, args.item_id, args.magazine_id, args.key, settings.library_dir
        )
    except (ValueError, FileExistsError) as exc:
        print(f"not resolved: {exc}", file=out)
        return 1
    print(
        f"{item.source_path}\n -> {item.target_path}\nApply with: python -m periodicool "
        f"apply {item.scan_id}",
        file=out,
    )
    return 0


def cmd_config(session, settings, args, out) -> int:
    if args.action == "get":
        print(json.dumps(library.get_setting(session, args.key)), file=out)
        return 0
    if any(word in args.key.lower() for word in _SECRET_WORDS):
        print("secrets come from the environment (for example PROWLARR_API_KEY)", file=out)
        return 2
    try:
        value = json.loads(args.value)
    except TypeError, ValueError:
        value = args.value
    library.set_setting(session, args.key, value)
    print(f"{args.key} = {json.dumps(value)}", file=out)
    return 0


def _client(session, out):
    client = search.make_client(session, transport=TRANSPORT)
    if client is None:
        print(
            "Prowlarr is not configured: config set prowlarr.url URL, and set the "
            f"{search.API_KEY_ENV} environment variable",
            file=out,
        )
    return client


def cmd_indexers(session, settings, args, out) -> int:
    if not (client := _client(session, out)):
        return 2
    now = datetime.now(UTC)
    for lim in search.indexer_limits(session, client, now):
        q = (
            "no limit set"
            if lim.query_limit is None
            else f"{lim.queries_left}/{lim.query_limit} left"
        )
        g = "no limit set" if lim.grab_limit is None else f"{lim.grabs_left}/{lim.grab_limit} left"
        state = lim.unavailable(now) or "ok"
        print(f"{lim.id:3}  {lim.name}: queries {q}, grabs {g}; {state}", file=out)
    return 0


def _print_candidates(result, out) -> None:
    by_issue: dict = {}
    for rel, ev in result.candidates:
        by_issue.setdefault(ev.issue_key or "(no issue)", []).append((rel, ev))
    for key in sorted(by_issue, key=lambda k: (k == "(no issue)", k)):
        rows = sorted(by_issue[key], key=lambda pair: (not pair[1].accepted, pair[1].rank))
        print(f"== {key}", file=out)
        for i, (rel, ev) in enumerate(rows):
            if ev.accepted:
                mark = "ACCEPT*" if i == 0 else "ACCEPT "
                print(f"  {mark} {rel.title}  [{rel.indexer}]", file=out)
                for note in ev.notes:
                    print(f"            note: {note}", file=out)
            else:
                print(f"  REJECT  {rel.title}  [{rel.indexer}]", file=out)
                for reason in ev.reasons:
                    print(f"            - {reason}", file=out)


def cmd_search(session, settings, args, out) -> int:
    mag = session.get(Magazine, args.magazine_id)
    if mag is None:
        print(f"no magazine {args.magazine_id}", file=out)
        return 2
    if not (client := _client(session, out)):
        return 2
    try:
        result = search.search_magazine(
            session, client, mag, today_in(settings.tz), force=args.force
        )
    except ProwlarrError as exc:
        print(f"search not run: {exc}", file=out)
        return 1
    accepted = sum(1 for _, ev in result.candidates if ev.accepted)
    note = " (from cache; --force to search again)" if result.from_cache else ""
    print(f"{mag.title}: {len(result.candidates)} candidates, {accepted} accepted{note}", file=out)
    for name, why in result.skipped.items():
        print(f"  indexer {name} left out: {why}", file=out)
    _print_candidates(result, out)
    return 0


def cmd_rss(session, settings, args, out) -> int:
    if not (client := _client(session, out)):
        return 2
    try:
        r = search.rss_sync(session, client, today_in(settings.tz))
    except ProwlarrError as exc:
        print(f"RSS sync not run: {exc}", file=out)
        return 1
    print(f"{r.seen} releases seen, {r.new} new, {len(r.accepted)} accepted", file=out)
    for name, why in r.skipped.items():
        print(f"  indexer {name} left out: {why}", file=out)
    for err in r.errors:
        print(f"  error: {err}", file=out)
    for rel in r.accepted:
        print(f"  ACCEPT  {rel.issue_key}  {rel.title}", file=out)
        for reason in rel.reasons:
            print(f"            {reason}", file=out)
    return 0


def cmd_wanted(session, settings, args, out) -> int:
    today = today_in(settings.tz)
    for mag in session.scalars(select(Magazine).where(Magazine.monitored.is_(True))):
        sync_expected_issues(session, mag, today)
        issues = search.wanted_issues(session, mag, today)
        state = session.get(SearchState, mag.id)
        nxt = state.next_search_at if state and state.next_search_at else "now"
        print(f"{mag.title}: {len(issues)} wanted; next search {nxt}", file=out)
        for issue in issues:
            print(f"    {issue.key:16} {effective_status(issue, mag, today)}", file=out)
    return 0


def cmd_jobs(session, settings, args, out) -> int:
    for job in session.scalars(select(Job).order_by(Job.name)):
        print(
            f"{job.name:8} every {job.interval_s}s  last {job.last_finished_at}  "
            f"next {job.next_run_at}\n         {job.last_result}",
            file=out,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="periodicool")
    sub = p.add_subparsers(dest="command")
    mags = sub.add_parser("magazines").add_subparsers(dest="mag_command", required=True)
    imp = mags.add_parser("import")
    imp.add_argument("file")
    imp.set_defaults(func=cmd_magazines_import)
    mags.add_parser("list").set_defaults(func=cmd_magazines_list)
    scan = sub.add_parser("scan")
    scan.add_argument("root", nargs="?")
    scan.add_argument("--import", dest="import_folder", action="store_true")
    scan.set_defaults(func=cmd_scan)
    apply = sub.add_parser("apply")
    apply.add_argument("scan_id", type=int)
    apply.set_defaults(func=cmd_apply)
    sub.add_parser("queue").set_defaults(func=cmd_queue)
    sk = sub.add_parser("skip")
    sk.add_argument("item_id", type=int)
    sk.set_defaults(func=cmd_skip)
    res = sub.add_parser("resolve")
    res.add_argument("item_id", type=int)
    res.add_argument("magazine_id", type=int)
    res.add_argument("key")
    res.set_defaults(func=cmd_resolve)
    cfg = sub.add_parser("config")
    cfg.add_argument("action", choices=["get", "set"])
    cfg.add_argument("key")
    cfg.add_argument("value", nargs="?")
    cfg.set_defaults(func=cmd_config)
    sub.add_parser("indexers").set_defaults(func=cmd_indexers)
    se = sub.add_parser("search")
    se.add_argument("magazine_id", type=int)
    se.add_argument("--force", action="store_true", help="ignore cached results")
    se.set_defaults(func=cmd_search)
    sub.add_parser("rss").set_defaults(func=cmd_rss)
    sub.add_parser("wanted").set_defaults(func=cmd_wanted)
    sub.add_parser("jobs").set_defaults(func=cmd_jobs)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None, out=None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or load_settings()
    out = out or sys.stdout
    if args.command is None:
        import uvicorn

        from periodicool.main import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0
    with _session(settings) as session:
        return args.func(session, settings, args, out)
