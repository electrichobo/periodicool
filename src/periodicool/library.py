"""Library scan, organize and manual import queue.

Safety rules (CLAUDE.md):
- Planning never touches files. Applying moves and renames only.
- Nothing is ever overwritten. A taken target sends the newcomer to review.
- Library files are never deleted. The only unlink calls remove the old name
  of a file that was just hard-linked to its new name (a rename), the source of
  an import-folder file after a verified copy, or our own temp file.
- Every move attempt is logged in `file_moves` and the log.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool.matching import match_title
from periodicool.models import (
    FileMove,
    FormatProfile,
    Frequency,
    ImportItem,
    Issue,
    IssueStatus,
    ItemAction,
    ItemStatus,
    Magazine,
    ReasonCode,
    Scan,
    ScanKind,
    Setting,
)
from periodicool.naming import DEFAULT_TEMPLATE, UnsafePathError, inside, render
from periodicool.parser import FORMATS, parse_release
from periodicool.schedule import SEASON_MONTHS, identify

log = logging.getLogger(__name__)

TEMPLATE_KEY = "naming.template"
FIRST_SCAN_KEY = "library.first_scan_applied"
AUTO_APPLY_KEY = "import.auto_apply"
_PARTIAL = (".part", ".tmp", "~", ".!qb", ".crdownload")
_OPEN = (ItemStatus.PENDING, ItemStatus.SKIPPED, ItemStatus.FAILED)


# Settings


def get_setting(session: Session, key: str, default=None):
    row = session.get(Setting, key)
    return default if row is None else row.value


def set_setting(session: Session, key: str, value) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


# Walking and classifying


def walk(root: Path) -> tuple[list[Path], int]:
    """Supported files under root, and the count of other files left alone."""
    found, ignored = [], 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name.startswith(".") or name.endswith(_PARTIAL) or path.is_symlink():
                ignored += 1
                continue
            if path.suffix.lower().lstrip(".") in FORMATS:
                found.append(path)
            else:
                ignored += 1
    return found, ignored


@dataclass
class Decision:
    action: ItemAction
    reason_code: ReasonCode | None = None
    reason: str | None = None
    magazine: Magazine | None = None
    issue_key: str | None = None
    target: str | None = None  # relative to the library root, POSIX
    parsed: dict = field(default_factory=dict)

    def review(self, code: ReasonCode, reason: str) -> None:
        self.action, self.reason_code, self.reason = ItemAction.REVIEW, code, reason
        self.target = None


def _jsonable(fields: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items()}


def _candidates(path: Path, root: Path) -> list[str]:
    """The file stem, then with the parent and grandparent folder names in front."""
    names = [path.stem]
    parents = [p for p in path.parents if p != root and p.is_relative_to(root)][:2]
    prefix = ""
    for parent in parents:
        prefix = f"{parent.name} {prefix}"
        names.append(f"{prefix}{path.stem}")
    return names


def classify(path: Path, library_root: Path, scan_root: Path, magazines, template: str) -> Decision:
    ext = path.suffix.lower().lstrip(".")
    first = None
    near: list[str] = []
    for name in _candidates(path, scan_root):
        parsed = parse_release(name)
        first = first or parsed
        results = [(m, match_title(parsed, m.title_spec())) for m in magazines]
        matches = [m for m, r in results if r.accepted]
        # Title matched but region or language did not: say so instead of "no magazine".
        near += [f"{m.title}: {r.reason}" for m, r in results if not r.accepted
                 and not r.reason.startswith(("title", "no title"))]  # fmt: skip
        if len(matches) > 1:
            titles = ", ".join(m.title for m in matches)
            d = Decision(ItemAction.REVIEW, parsed={"name": name, "title": parsed.title})
            d.review(ReasonCode.AMBIGUOUS_MAGAZINE, f"matches {titles}")
            return d
        if matches:
            return _decide(parsed, name, matches[0], ext, library_root, template)
    d = Decision(ItemAction.REVIEW, parsed={"name": path.stem, "title": first.title})
    if not first.title:
        d.review(ReasonCode.UNPARSED, "no title in the name")
    elif near:
        d.review(ReasonCode.NO_MAGAZINE, "; ".join(dict.fromkeys(near)))
    else:
        d.review(ReasonCode.NO_MAGAZINE, f"no tracked magazine matches {first.title!r}")
    return d


def _decide(parsed, name, mag, ext, library_root, template) -> Decision:
    summary = {
        "name": name,
        "title": parsed.title,
        "format": ext,
        "true_pdf": parsed.true_pdf,
        "reasons": list(parsed.reasons),
    }
    d = Decision(ItemAction.MOVE, magazine=mag, parsed=summary)
    if parsed.reasons:
        d.review(ReasonCode.LOW_CONFIDENCE, "; ".join(parsed.reasons))
        return d
    ident = identify(parsed, mag)
    summary |= {"fields": _jsonable(ident.fields), "notes": list(ident.notes)}
    if not ident.confident:
        d.review(ReasonCode.UNIDENTIFIED, "; ".join(ident.reasons) or "no issue key")
        return d
    d.issue_key = ident.key
    _set_target(d, mag, ext, library_root, template)
    return d


def _set_target(d: Decision, mag: Magazine, ext: str, library_root: Path, template: str) -> None:
    try:
        rel = render(template, mag, d.parsed.get("fields", {}), ext, key=d.issue_key)
        inside(library_root, rel)
    except UnsafePathError as exc:
        d.review(ReasonCode.OUTSIDE_ROOT, str(exc))
        return
    d.target = rel.as_posix()


# Planning


def _format_score(d: Decision, profile: FormatProfile | None) -> tuple[int, int]:
    formats = profile.formats if profile else list(FORMATS)
    fmt = d.parsed.get("format")
    rank = formats.index(fmt) if fmt in formats else len(formats)
    prefer_true = profile.prefer_true_pdf if profile else True
    return rank, 0 if (prefer_true and d.parsed.get("true_pdf")) else 1


def _second_issue_keys(group: list[tuple[str, Decision]], existing: Issue | None) -> None:
    """Thirteen-per-year: different issue numbers in one month get keys -2, -3."""
    numbers = {d.parsed.get("fields", {}).get("issue_number") for _, d in group}
    if existing is not None:
        numbers.add(existing.issue_number)
    if None in numbers or len(numbers) < 2:
        return
    ordered = sorted(numbers)
    for _, d in group:
        n = d.parsed["fields"]["issue_number"]
        idx = ordered.index(n)
        if idx:
            d.issue_key = f"{d.issue_key}-{idx + 1}"


def _resolve_conflicts(
    session: Session, decisions: list[tuple[str, Decision]], library_root: Path, template: str
) -> None:
    groups: dict[tuple[int, str], list[tuple[str, Decision]]] = {}
    for src, d in decisions:
        if d.action is not ItemAction.REVIEW:
            groups.setdefault((d.magazine.id, d.issue_key), []).append((src, d))

    # Thirteen-per-year months holding two different issues get split first.
    for (mag_id, key), group in list(groups.items()):
        mag = group[0][1].magazine
        if mag.frequency is Frequency.THIRTEEN_PER_YEAR:
            existing = _issue(session, mag_id, key)
            before = {id(d): d.issue_key for _, d in group}
            _second_issue_keys(group, existing)
            if any(d.issue_key != before[id(d)] for _, d in group):
                del groups[(mag_id, key)]
                for src, d in group:
                    _set_target(d, mag, d.parsed["format"], library_root, template)
                    if d.action is not ItemAction.REVIEW:
                        groups.setdefault((mag_id, d.issue_key), []).append((src, d))

    for (mag_id, key), group in groups.items():
        mag = group[0][1].magazine
        issue = _issue(session, mag_id, key)
        linked = issue.file_path if issue and issue.file_path else None
        incumbent = next((d for _, d in group if d.action is ItemAction.KEEP), None)
        if (
            linked
            and (library_root / linked).exists()
            and all(d.target != linked for _, d in group)
        ):
            for _, d in group:
                d.review(ReasonCode.DUPLICATE, f"issue already has {linked}; existing file kept")
            continue
        if len(group) == 1:
            continue
        if incumbent is not None:
            for _, d in group:
                if d is not incumbent:
                    d.review(
                        ReasonCode.DUPLICATE, f"duplicate of {incumbent.target}; existing kept"
                    )
            continue
        scored = sorted(group, key=lambda sd: _format_score(sd[1], mag.format_profile))
        best = _format_score(scored[0][1], mag.format_profile)
        if _format_score(scored[1][1], mag.format_profile) == best:
            names = ", ".join(Path(s).name for s, _ in group)
            for _, d in group:
                d.review(ReasonCode.DUPLICATE, f"same issue, equal format: {names}")
            continue
        winner = Path(scored[0][0]).name
        for _, d in scored[1:]:
            d.review(ReasonCode.DUPLICATE, f"{winner} is the better format")

    # A target that exists on disk (and is not this file) or is claimed twice.
    claimed: set[str] = set()
    for src, d in decisions:
        if d.action is not ItemAction.MOVE:
            if d.action is ItemAction.KEEP:
                claimed.add(d.target)
            continue
        target = library_root / d.target
        if d.target in claimed or (target.exists() and not _same_file(Path(src), target)):
            d.review(ReasonCode.COLLISION, f"{d.target} already exists; both files kept")
        else:
            claimed.add(d.target)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def _issue(session: Session, mag_id: int, key: str) -> Issue | None:
    return session.scalars(
        select(Issue).where(Issue.magazine_id == mag_id, Issue.key == key)
    ).one_or_none()


def plan_scan(
    session: Session, root: Path, kind: ScanKind, library_root: Path | None = None
) -> Scan:
    """Plan moves for every file under root. Writes database rows only."""
    root = Path(root).resolve()
    library_root = Path(library_root or root).resolve()
    template = get_setting(session, TEMPLATE_KEY, DEFAULT_TEMPLATE)
    magazines = list(session.scalars(select(Magazine)))
    scan = Scan(kind=kind, root=str(root), counts={})
    session.add(scan)
    session.flush()

    existing = {
        i.source_path: i
        for i in session.scalars(select(ImportItem))
        if Path(i.source_path).is_relative_to(root)
    }
    files, ignored = walk(root)
    counts = {"move": 0, "keep": 0, "review": 0, "ignored": ignored, "unchanged": 0}
    decisions: list[tuple[str, Decision]] = []
    stats = {}
    for path in files:
        src = str(path)
        st = path.stat()
        stats[src] = st
        item = existing.get(src)
        if (
            item
            and item.size == st.st_size
            and item.mtime == st.st_mtime
            and (
                item.status is ItemStatus.SKIPPED
                or (item.status is ItemStatus.PENDING and item.parsed.get("resolved"))
            )
        ):
            item.scan_id = scan.id
            counts["unchanged"] += 1
            continue
        d = classify(path, library_root, root, magazines, template)
        if d.action is ItemAction.MOVE and _same_file(path, library_root / d.target):
            d.action = ItemAction.KEEP
        decisions.append((src, d))

    _resolve_conflicts(session, decisions, library_root, template)

    for src, d in decisions:
        counts[d.action.value] += 1
        item = existing.get(src) or ImportItem(source_path=src)
        item.scan_id, item.origin = scan.id, kind
        item.size, item.mtime = stats[src].st_size, stats[src].st_mtime
        item.parsed = d.parsed
        item.magazine_id = d.magazine.id if d.magazine else None
        item.issue_key, item.target_path = d.issue_key, d.target
        item.action, item.reason_code, item.reason = d.action, d.reason_code, d.reason
        item.status = ItemStatus.PENDING
        session.add(item)

    seen = set(stats)
    for src, item in existing.items():
        if src not in seen and item.status in _OPEN and not Path(src).exists():
            item.status = ItemStatus.GONE

    scan.counts = counts
    scan.finished_at = datetime.now(UTC)
    session.flush()
    return scan


# Applying


class MoveError(Exception):
    pass


def _move(src: Path, dst: Path, allow_copy: bool) -> str:
    """Move src to dst without ever replacing dst. Returns the method used."""
    if dst.exists():
        if _same_file(src, dst):  # case-only rename on a case-insensitive disk
            src.rename(dst)
            return "rename"
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # fails if dst exists: no overwrite
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            if not allow_copy:
                raise MoveError("target is on another filesystem") from exc
            return _copy_then_remove(src, dst)
        if dst.exists():
            raise FileExistsError(dst) from exc
        src.rename(dst)  # hard links not supported here
        return "rename"
    src.unlink()  # removes the old name only; the data lives on at dst
    return "link"


def _copy_then_remove(src: Path, dst: Path) -> str:
    tmp = dst.with_name(f".{dst.name}.periodicool-tmp")
    try:
        with src.open("rb") as fin, tmp.open("xb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        if tmp.stat().st_size != src.stat().st_size:
            raise MoveError("copy size mismatch")
        if dst.exists():
            raise FileExistsError(dst)
        tmp.rename(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)  # our own partial copy
        raise
    src.unlink()  # import-folder source, removed only after a verified copy
    return "copy"


def _fields_for_issue(item: ImportItem) -> dict:
    fields = dict(item.parsed.get("fields", {}))
    cover = fields.get("cover_date")
    out = {
        k: fields.get(k)
        for k in ("year", "month", "month_end", "day", "season", "issue_number", "volume_number")
    }
    out["special"] = bool(fields.get("special"))
    out["cover_date"] = date.fromisoformat(cover) if cover else None
    return out


def _link_issue(session: Session, item: ImportItem) -> None:
    issue = _issue(session, item.magazine_id, item.issue_key)
    if issue is None:
        issue = Issue(
            magazine_id=item.magazine_id,
            key=item.issue_key,
            expected=False,
            monitored=True,
            **_fields_for_issue(item),
        )
        session.add(issue)
    issue.file_path = item.target_path
    issue.status = IssueStatus.IMPORTED


def apply_scan(
    session: Session, scan_id: int, library_root: Path, item_ids: list[int] | None = None
) -> dict:
    """Carry out a scan's pending move and keep items. Returns counts."""
    library_root = Path(library_root).resolve()
    query = select(ImportItem).where(
        ImportItem.scan_id == scan_id,
        ImportItem.status == ItemStatus.PENDING,
        ImportItem.action.in_([ItemAction.MOVE, ItemAction.KEEP]),
    )
    if item_ids is not None:
        query = query.where(ImportItem.id.in_(item_ids))
    result = {"moved": 0, "kept": 0, "failed": 0, "review": 0}
    for item in session.scalars(query).all():
        apply_item(session, item, library_root, result)
    scan = session.get(Scan, scan_id)
    scan.applied_at = datetime.now(UTC)
    if scan.kind is ScanKind.LIBRARY and (result["moved"] or result["kept"]):
        set_setting(session, FIRST_SCAN_KEY, True)
    session.flush()
    return result


def apply_item(session: Session, item: ImportItem, library_root: Path, result: dict) -> None:
    src = Path(item.source_path)
    try:
        st = src.stat()
    except FileNotFoundError:
        item.status, item.reason = ItemStatus.GONE, "source no longer exists"
        result["failed"] += 1
        return
    if st.st_size != item.size or st.st_mtime != item.mtime:
        item.status, item.reason = ItemStatus.FAILED, "source changed since the scan; rescan"
        result["failed"] += 1
        return
    if item.action is ItemAction.KEEP:
        _link_issue(session, item)
        item.status = ItemStatus.DONE
        result["kept"] += 1
        return
    try:
        dst = inside(library_root, Path(item.target_path))
    except UnsafePathError as exc:
        item.action, item.reason_code, item.reason = (
            ItemAction.REVIEW,
            ReasonCode.OUTSIDE_ROOT,
            str(exc),
        )
        result["review"] += 1
        return
    move = FileMove(item_id=item.id, source=str(src), target=str(dst), ok=False)
    try:
        move.method = _move(
            src, dst, allow_copy=item.origin in (ScanKind.IMPORT, ScanKind.DOWNLOAD)
        )
    except FileExistsError:
        move.error = "target exists"
        item.action, item.reason_code = ItemAction.REVIEW, ReasonCode.COLLISION
        item.reason = f"{item.target_path} appeared after the scan; both files kept"
        result["review"] += 1
        log.warning("Not moved, target exists: %s -> %s", src, dst)
    except (OSError, MoveError) as exc:
        move.error = str(exc)
        item.status, item.reason = ItemStatus.FAILED, f"move failed: {exc}"
        result["failed"] += 1
        log.error("Move failed: %s -> %s: %s", src, dst, exc)
    else:
        move.ok = True
        _link_issue(session, item)
        item.status = ItemStatus.DONE
        result["moved"] += 1
        log.info("Moved (%s): %s -> %s", move.method, src, dst)
    session.add(move)


# Manual import queue


def queue(session: Session) -> list[ImportItem]:
    return list(
        session.scalars(
            select(ImportItem)
            .where(ImportItem.status == ItemStatus.PENDING, ImportItem.action == ItemAction.REVIEW)
            .order_by(ImportItem.id)
        )
    )


def skip(session: Session, item_id: int) -> ImportItem:
    item = session.get(ImportItem, item_id)
    item.status = ItemStatus.SKIPPED
    return item


_KEY_PATTERNS = (
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "day"),
    (re.compile(r"(\d{4})-(\d{2})(?:-\d+)?"), "month"),
    (re.compile(r"(\d{4})-(spring|summer|autumn|winter)"), "season"),
    (re.compile(r"n(\d+)"), "issue"),
    (re.compile(r"v(\d+)"), "volume"),
)


def fields_from_key(key: str, mag: Magazine) -> dict:
    """Issue fields from a key typed by the user (2023-11, 2017-12-04, n437, v41...)."""
    for pattern, kind in _KEY_PATTERNS:
        m = pattern.fullmatch(key)
        if not m:
            continue
        if kind == "day" and mag.frequency is not Frequency.THIRTEEN_PER_YEAR:
            y, mo, d = map(int, m.groups())
            return {"year": y, "month": mo, "day": d, "cover_date": date(y, mo, d).isoformat()}
        if kind in ("day", "month"):
            y, mo = int(m.group(1)), int(m.group(2))
            return {"year": y, "month": mo, "cover_date": date(y, mo, 1).isoformat()}
        if kind == "season":
            y = int(m.group(1))
            cover = date(y, SEASON_MONTHS[m.group(2)], 1)
            return {"year": y, "season": m.group(2), "cover_date": cover.isoformat()}
        return {f"{kind}_number": int(m.group(1))}
    raise ValueError(f"not an issue key: {key!r}")


def resolve(
    session: Session, item_id: int, magazine_id: int, issue_key: str, library_root: Path
) -> ImportItem:
    """The user matched a queued file to an issue. Applying it is still a separate step."""
    item = session.get(ImportItem, item_id)
    mag = session.get(Magazine, magazine_id)
    if item is None or mag is None:
        raise ValueError("no such item or magazine")
    template = get_setting(session, TEMPLATE_KEY, DEFAULT_TEMPLATE)
    fields = fields_from_key(issue_key, mag)
    ext = Path(item.source_path).suffix.lower().lstrip(".")
    rel = render(template, mag, fields, ext, key=issue_key)
    target = inside(Path(library_root), rel)
    if target.exists() and not _same_file(Path(item.source_path), target):
        raise FileExistsError(f"{rel.as_posix()} already exists")
    item.parsed = dict(item.parsed) | {"fields": fields, "resolved": True, "format": ext}
    item.magazine_id, item.issue_key, item.target_path = mag.id, issue_key, rel.as_posix()
    item.action, item.reason_code = ItemAction.MOVE, None
    item.reason = "matched by user"
    item.status = ItemStatus.PENDING
    return item


def process_import_folder(session: Session, import_root: Path, library_root: Path) -> Scan:
    """Plan the drop-in folder; apply confident moves once the library has been organized."""
    scan = plan_scan(session, import_root, ScanKind.IMPORT, library_root)
    if get_setting(session, AUTO_APPLY_KEY, True) and get_setting(session, FIRST_SCAN_KEY, False):
        apply_scan(session, scan.id, library_root)
    return scan
