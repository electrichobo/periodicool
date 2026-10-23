"""Backups of /config: the database (a consistent copy through SQLite's backup API),
secret.key and api.key, zipped into /config/backups. Restore checks the copy first
and backs up the current database before swapping it in."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from periodicool.config import DB_FILENAME

BACKUP_DIR = "backups"
FOLDER_KEY = "backup.folder"
# A folder chosen in System, Backups; loaded at start and on save. None: /config/backups.
FOLDER: dict[str, str | None] = {"path": None}
EXTRA_FILES = ("secret.key", "api.key")
NAME = re.compile(r"periodicool-\d{8}-\d{6}-\d{6}(?:-[a-z]+)?\.zip")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupFile:
    name: str
    size: int
    created: datetime


def backup_dir(config_dir: Path) -> Path:
    return Path(FOLDER["path"]) if FOLDER["path"] else config_dir / BACKUP_DIR


def list_backups(config_dir: Path) -> list[BackupFile]:
    folder = backup_dir(config_dir)
    if not folder.is_dir():
        return []
    out = []
    for path in folder.iterdir():
        if path.is_file() and NAME.fullmatch(path.name):
            st = path.stat()
            out.append(BackupFile(path.name, st.st_size, datetime.fromtimestamp(st.st_mtime, UTC)))
    return sorted(out, key=lambda b: b.name, reverse=True)


def backup_path(config_dir: Path, name: str) -> Path:
    """The file for a listed backup name. Anything else is refused."""
    if not NAME.fullmatch(name):
        raise BackupError("not a backup file name")
    path = backup_dir(config_dir) / name
    if not path.is_file():
        raise BackupError("no such backup")
    return path


def delete_backup(config_dir: Path, name: str) -> None:
    """Delete one listed backup. Only files named like our backups, in the backup folder."""
    backup_path(config_dir, name).unlink()


def _copy_database(db_path: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(config_dir: Path, keep: int = 7, label: str = "") -> BackupFile:
    folder = backup_dir(config_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    name = f"periodicool-{stamp}{'-' + label if label else ''}.zip"
    target = folder / name
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / DB_FILENAME
        _copy_database(config_dir / DB_FILENAME, copy)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(copy, DB_FILENAME)
            for extra in EXTRA_FILES:
                if (config_dir / extra).is_file():
                    zf.write(config_dir / extra, extra)
    target.chmod(0o600)
    prune(config_dir, keep)
    return next(b for b in list_backups(config_dir) if b.name == name)


def prune(config_dir: Path, keep: int) -> list[str]:
    """Keep the newest backups; the ones made before a restore are kept too."""
    removed = []
    regular = [b for b in list_backups(config_dir) if not b.name.endswith("-prerestore.zip")]
    for old in regular[max(keep, 1) :]:
        (backup_dir(config_dir) / old.name).unlink()
        removed.append(old.name)
    return removed


def check_backup(path: Path, workdir: Path) -> Path:
    """Unpack the database from a backup and check it. Returns the checked copy."""
    try:
        with zipfile.ZipFile(path) as zf:
            if DB_FILENAME not in zf.namelist():
                raise BackupError("the backup holds no database")
            zf.extract(DB_FILENAME, workdir)
    except zipfile.BadZipFile as exc:
        raise BackupError("not a zip file") from exc
    copy = workdir / DB_FILENAME
    try:
        conn = sqlite3.connect(copy)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"the database is damaged: {exc}") from exc
    if result != "ok":
        raise BackupError(f"the database is damaged: {result}")
    if "alembic_version" not in tables or "magazines" not in tables:
        raise BackupError("this is not a PeriodiCool database")
    # Bring it up to this version's schema before it goes live.
    from periodicool import db

    db.upgrade(f"sqlite:///{copy.as_posix()}")
    return copy


def restore_backup(config_dir: Path, name: str, engine) -> str:
    """Swap in the backup's database. The current one is backed up first.

    Returns the name of that safety backup.
    """
    path = backup_path(config_dir, name)
    with tempfile.TemporaryDirectory() as tmp:
        checked = check_backup(path, Path(tmp))
        safety = create_backup(config_dir, keep=10_000, label="prerestore")
        engine.dispose()
        live = config_dir / DB_FILENAME
        for suffix in ("-wal", "-shm"):
            Path(f"{live}{suffix}").unlink(missing_ok=True)
        shutil.copyfile(checked, live)
        with zipfile.ZipFile(path) as zf:
            for extra in EXTRA_FILES:
                if extra in zf.namelist():
                    (config_dir / extra).write_bytes(zf.read(extra))
                    (config_dir / extra).chmod(0o600)
    return safety.name
