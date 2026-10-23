"""Magazine covers taken from the newest issue's PDF, for the cards.

When a magazine has no cover image of its own in the library (cover.jpg and the
like), the largest JPEG on page 1 of its newest PDF issue is copied into
/config/covers. That is PeriodiCool's own cache: library files are only read.
Only JPEG images are used (browsers show them as they are, no image library
needed); a cover drawn some other way is skipped. Reading is time-limited.
"""

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from periodicool.models import Issue, Magazine

log = logging.getLogger(__name__)

COVERS_DIR = "covers"
TIMEOUT_S = 5.0
MIN_SIDE = 200  # smaller images are logos or icons, not covers
MAX_BYTES = 15 * 1024 * 1024


def covers_dir(config_dir: Path) -> Path:
    return Path(config_dir) / COVERS_DIR


def _filters(obj) -> list[str]:
    raw = obj.get("/Filter")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(f) for f in items]


def _largest_jpeg(resources, depth: int = 0):
    best = None
    if resources is None:
        return None
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return None
    for ref in xobjects.get_object().values():
        obj = ref.get_object()
        kind = obj.get("/Subtype")
        if kind == "/Image" and _filters(obj) == ["/DCTDecode"]:
            width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
            if min(width, height) >= MIN_SIDE and (best is None or width * height > best[0]):
                best = (width * height, obj)
        elif kind == "/Form" and depth < 3:
            inner = _largest_jpeg(obj.get("/Resources"), depth + 1)
            if inner is not None and (best is None or inner[0] > best[0]):
                best = inner
    return best


def _cover_jpeg(path: Path) -> bytes | None:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001  needs a password or a crypto library
            return None
    if not reader.pages:
        return None
    best = _largest_jpeg(reader.pages[0].get("/Resources"))
    if best is None:
        return None
    data = best[1]._data  # a DCTDecode stream is the JPEG file itself
    if not data or data[:2] != b"\xff\xd8" or len(data) > MAX_BYTES:
        return None
    return bytes(data)


def cover_jpeg(path: Path, timeout: float = TIMEOUT_S) -> bytes | None:
    """The cover JPEG on page 1 of a PDF, or None."""
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_cover_jpeg, path)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        log.info("Cover skipped, took over %.0f s: %s", timeout, path.name)
    except Exception as exc:  # noqa: BLE001  a bad PDF must not stop a refresh
        log.info("Cover unreadable (%s): %s", type(exc).__name__, path.name)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return None


def _newest_pdf(session: Session, mag: Magazine) -> Issue | None:
    return session.scalars(
        select(Issue)
        .where(
            Issue.magazine_id == mag.id,
            Issue.file_path.is_not(None),
            Issue.file_path.ilike("%.pdf"),
        )
        .order_by(Issue.cover_date.desc().nulls_last(), Issue.id.desc())
        .limit(1)
    ).first()


def cached_cover(config_dir: Path, mag_id: int) -> Path | None:
    folder = covers_dir(config_dir)
    found = sorted(folder.glob(f"{mag_id}-*.jpg")) if folder.is_dir() else []
    return found[-1] if found else None


def with_pdf_covers(config_dir: Path) -> set[int]:
    """Magazine ids that have a cached cover."""
    folder = covers_dir(config_dir)
    if not folder.is_dir():
        return set()
    return {
        int(p.name.split("-")[0]) for p in folder.glob("*-*.jpg") if p.name.split("-")[0].isdigit()
    }


def refresh_covers(
    session: Session, library_root: Path, config_dir: Path, magazine_id: int | None = None
) -> int:
    """Cache the newest issue's cover for each magazine without a cover image of its own
    (or only for one magazine). Returns how many covers were taken from PDFs. A newest
    issue without a usable cover leaves a small marker, so it is not read again."""
    folder = covers_dir(config_dir)
    made = 0
    query = select(Magazine)
    if magazine_id is not None:
        query = query.where(Magazine.id == magazine_id)
    for mag in session.scalars(query):
        if mag.cover_path:
            continue
        issue = _newest_pdf(session, mag)
        if issue is None:
            continue
        target = folder / f"{mag.id}-{issue.id}.jpg"
        tried = folder / f"{mag.id}-{issue.id}.none"
        if target.is_file() or tried.is_file():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        data = cover_jpeg(Path(library_root) / issue.file_path)
        keep = target if data is not None else tried
        keep.write_bytes(data or b"")
        for old in [*folder.glob(f"{mag.id}-*.jpg"), *folder.glob(f"{mag.id}-*.none")]:
            if old != keep:
                old.unlink(missing_ok=True)  # our own cache of an older issue
        made += data is not None
    return made
