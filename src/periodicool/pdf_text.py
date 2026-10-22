"""A quick look inside a PDF: the text of its first pages, searched for an ISSN,
a cover date and an issue number. Used only when a file name is not enough.

Reading never fails a scan or an import. Encrypted, damaged or image-only files,
and files that take too long, simply give no clues.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from periodicool.issn import normalize
from periodicool.parser import text_clues

log = logging.getLogger(__name__)
# pypdf warns about every odd file it opens; a scan of a big library would flood the log.
logging.getLogger("pypdf").setLevel(logging.ERROR)

DEFAULT_PAGES = 2
TIMEOUT_S = 5.0
MAX_TEXT = 20_000
ENABLED_KEY = "pdf.read_text"
PAGES_KEY = "pdf.pages"
# "ISSN 0749-3851", "ISSN: 0749 3851", "ISSN (print) 0749-3851".
_ISSN = re.compile(
    r"\bISSN(?![a-z])[^\dA-Za-z]{0,6}(?:[A-Za-z]+[^\dA-Za-z]{1,4})?"
    r"(\d{4}\s*[-\u2010\u2013]?\s*\d{3}[\dXx])\b"
)


@dataclass(frozen=True)
class PdfClues:
    issns: tuple[str, ...] = ()
    year: int | None = None
    month: int | None = None
    month_end: int | None = None
    season: str | None = None
    issue: int | None = None
    volume: int | None = None
    text: str = ""  # first pages' text, for title search; not stored

    def as_dict(self) -> dict:
        data = {k: v for k, v in asdict(self).items() if v not in (None, (), "") and k != "text"}
        if "issns" in data:
            data["issns"] = list(data["issns"])
        return data

    def summary(self) -> str:
        parts = [f"ISSN {i}" for i in self.issns]
        if self.season and self.year:
            parts.append(f"{self.season.capitalize()} {self.year}")
        elif self.month and self.year:
            parts.append(f"{self.year}-{self.month:02d}")
        if self.issue is not None:
            parts.append(f"issue {self.issue}")
        if self.volume is not None:
            parts.append(f"volume {self.volume}")
        return ", ".join(parts) or "no clues"


def _read_text(path: Path, pages: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001  needs a password or a crypto library
            return ""
    chunks = []
    for page in reader.pages[: max(1, pages)]:
        chunks.append(page.extract_text() or "")
        if sum(len(c) for c in chunks) > MAX_TEXT:
            break
    return "\n".join(chunks)[:MAX_TEXT]


def clues_from_text(text: str) -> PdfClues:
    issns = []
    for m in _ISSN.finditer(text):
        if (code := normalize(re.sub(r"[\s‐–]", "", m.group(1)))) and code not in issns:
            issns.append(code)
    found = text_clues(text)
    return PdfClues(issns=tuple(issns), text=text, **found)


def read_clues(path: Path, pages: int = DEFAULT_PAGES, timeout: float = TIMEOUT_S):
    """PdfClues for a PDF, or None when it cannot be read in time."""
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_read_text, path, pages)
    try:
        text = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        log.info("PDF text skipped, took over %.0f s: %s", timeout, path.name)
        return None
    except Exception as exc:  # noqa: BLE001  a bad PDF must not stop a scan
        log.info("PDF text unreadable (%s): %s", type(exc).__name__, path.name)
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not text.strip():
        return None
    return clues_from_text(text)
