"""Usenet and Torrent Blackhole: Sonarr's folder clients, for downloaders PeriodiCool
cannot talk to.

PeriodiCool writes the .nzb or .torrent (or a .magnet file) into the drop folder; some
other program picks it up and leaves the finished download in the watch folder. An
item there counts as finished once it has not changed for the wait period.

Only items whose names PeriodiCool dropped are ever looked at. Nothing in the watch
folder is ever deleted here; the only file this client removes is its own drop file
while it is still waiting.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from periodicool.bencode import BencodeError, decode_torrent, info_hash, magnet_hash
from periodicool.clients.base import ClientError, JobState, JobStatus
from periodicool.naming import sanitize

PARTIAL = (".part", ".!qb", ".!ut", ".tmp", ".crdownload", ".partial", ".aria2")
DEFAULT_WAIT_S = 30


class BlackholeClient:
    row_id: int | None = None
    category = ""
    path_map: list = []
    options: dict = {}

    def __init__(
        self,
        protocol: str,
        drop_folder: str,
        watch_folder: str,
        downloads_dir: Path | None = None,
        save_magnets: bool = False,
        magnet_extension: str = ".magnet",
        wait_s: float = DEFAULT_WAIT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.protocol = protocol
        self.name = "usenet_blackhole" if protocol == "usenet" else "torrent_blackhole"
        self.display_name = "Usenet Blackhole" if protocol == "usenet" else "Torrent Blackhole"
        self.drop = Path(drop_folder) if drop_folder else None
        self.watch = Path(watch_folder) if watch_folder else None
        self.downloads_dir = downloads_dir
        self.save_magnets = save_magnets
        ext = (magnet_extension or ".magnet").strip()
        self.magnet_extension = ext if ext.startswith(".") else f".{ext}"
        self.wait_s = wait_s
        self._clock = clock

    # Setup

    def _folders(self) -> tuple[Path, Path]:
        if self.drop is None or self.watch is None:
            raise ClientError("both folders are needed")
        return self.drop, self.watch

    def check(self, category: str = "") -> str:
        drop, watch = self._folders()
        what = "NZB" if self.protocol == "usenet" else "torrent"
        if not drop.is_dir():
            raise ClientError(f"the {what} folder {drop} does not exist")
        if not os.access(drop, os.W_OK):
            raise ClientError(f"PeriodiCool cannot write to the {what} folder {drop}")
        if not watch.is_dir():
            raise ClientError(f"the watch folder {watch} does not exist")
        if not os.access(watch, os.R_OK | os.X_OK):
            raise ClientError(f"PeriodiCool cannot read the watch folder {watch}")
        if self.downloads_dir is not None:
            downloads = self.downloads_dir.resolve()
            if not watch.resolve().is_relative_to(downloads):
                raise ClientError(
                    f"the watch folder {watch} is outside the downloads folder {downloads}; "
                    "imports only read from there"
                )
        return "folders ready"

    # Adding

    def _write(self, name: str, data: bytes) -> None:
        """Write into the drop folder under a temporary name, then rename, so the
        downloader never reads half a file. Never overwrites."""
        drop, _ = self._folders()
        target = drop / name
        if target.exists():
            raise ClientError(f"{name} is already in the drop folder")
        temp = drop / f".{name}.periodicool-tmp"
        try:
            temp.write_bytes(data)
            if target.exists():
                raise ClientError(f"{name} is already in the drop folder")
            temp.rename(target)
        except OSError as exc:
            raise ClientError(f"cannot write {name}: {exc}") from exc
        finally:
            temp.unlink(missing_ok=True)

    def add_nzb(self, nzb: bytes, name: str, category: str = "", **_) -> str:
        if self.protocol != "usenet":
            raise ClientError("a Torrent Blackhole takes torrents, not NZB files")
        title = _clean(name)
        self._write(f"{title}.nzb", nzb)
        return title

    def add_torrent(
        self,
        name: str,
        category: str = "",
        torrent: bytes | None = None,
        magnet: str | None = None,
        **_,
    ) -> str:
        if self.protocol != "torrent":
            raise ClientError("a Usenet Blackhole takes NZB files, not torrents")
        try:
            if torrent is not None:
                meta, _raw = decode_torrent(torrent)
                info = meta[b"info"]
                title = _clean(info.get(b"name", b"").decode("utf-8", "replace") or name)
                job = info_hash(torrent)
                self._write(f"{title}.torrent", torrent)
            else:
                if not self.save_magnets:
                    raise ClientError(
                        "this release is a magnet link; turn on Save magnet files to use it"
                    )
                job = magnet_hash(magnet or "")
                shown = dict(urllib.parse.parse_qsl((magnet or "")[len("magnet:?") :])).get("dn")
                if not shown:
                    raise ClientError(
                        "the magnet link has no name, so its download could not be found again"
                    )
                title = _clean(shown)
                self._write(f"{title}{self.magnet_extension}", (magnet or "").encode())
        except BencodeError as exc:
            raise ClientError(str(exc)) from exc
        return f"{job}|{title}"

    # Watching

    def _drop_file(self, title: str) -> Path | None:
        drop, _ = self._folders()
        exts = (".nzb",) if self.protocol == "usenet" else (".torrent", self.magnet_extension)
        for ext in exts:
            if (drop / f"{title}{ext}").exists():
                return drop / f"{title}{ext}"
        return None

    def _item(self, title: str) -> Path | None:
        """Our item in the watch folder: a folder or file named after what we dropped."""
        _, watch = self._folders()
        want = title.lower()
        try:
            entries = list(watch.iterdir())
        except OSError as exc:
            raise ClientError(f"cannot read the watch folder: {exc}") from exc
        for entry in entries:
            name = entry.name.lower()
            if name == want or (entry.is_file() and entry.stem.lower() == want):
                return entry
        return None

    def jobs(self, job_ids: list[str], category: str = "") -> list[JobState]:
        out = []
        for job in job_ids:
            title = job.split("|", 1)[-1]
            item = self._item(title)
            if item is None:
                waiting = self._drop_file(title) is not None
                out.append(
                    JobState(
                        job_id=job,
                        name=title,
                        category=category,
                        status=JobStatus.QUEUED if waiting else JobStatus.DOWNLOADING,
                        client_status="waiting to be picked up"
                        if waiting
                        else "picked up, not in the watch folder yet",
                        progress=0.0,
                    )
                )
                continue
            newest, size, partial = _scan(item)
            settled = self._clock() - newest >= self.wait_s and not partial
            out.append(
                JobState(
                    job_id=job,
                    name=title,
                    category=category,
                    status=JobStatus.COMPLETED if settled else JobStatus.DOWNLOADING,
                    client_status="finished" if settled else "still changing",
                    progress=100.0 if settled else 0.0,
                    storage_path=str(item),
                    size_bytes=size,
                    size_left_bytes=0 if settled else None,
                    in_history=settled,
                )
            )
        return out

    def remove(self, job_id: str, category: str = "") -> bool:
        """Delete our own drop file if it is still waiting. The watch folder is never
        touched; a download already picked up may still finish in the other program."""
        dropped = self._drop_file(job_id.split("|", 1)[-1])
        if dropped is None:
            return False
        dropped.unlink(missing_ok=True)
        return True


def _clean(name: str) -> str:
    title = sanitize(name)[:200].strip(". ")
    if not title:
        raise ClientError("the release has no usable name for a file")
    return title


def _scan(item: Path) -> tuple[float, int, bool]:
    """Newest change time, total size, and whether a partial file is there."""
    stat = item.stat()
    newest, size, partial = stat.st_mtime, 0, False
    files = [item] if item.is_file() else [p for p in item.rglob("*") if p.is_file()]
    for f in files:
        st = f.stat()
        newest, size = max(newest, st.st_mtime), size + st.st_size
        partial = partial or f.name.lower().endswith(PARTIAL)
    return newest, size, partial
