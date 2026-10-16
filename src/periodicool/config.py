"""Environment settings.

Only paths, bind settings, process settings and secrets come from the environment.
Everything else is stored in the database and edited in the UI.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DB_FILENAME = "periodicool.db"


class ConfigError(ValueError):
    """Raised when an environment value is invalid."""


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    library_dir: Path
    downloads_dir: Path
    import_dir: Path | None
    host: str
    port: int
    puid: int
    pgid: int
    umask: int
    tz: str

    @property
    def db_path(self) -> Path:
        return self.config_dir / DB_FILENAME

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


def _int(env: Mapping[str, str], name: str, default: int, base: int = 10) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw, base)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    import_dir = env.get("PERIODICOOL_IMPORT_DIR", "").strip()
    port = _int(env, "PERIODICOOL_PORT", 8383)
    if not 1 <= port <= 65535:
        raise ConfigError(f"PERIODICOOL_PORT out of range: {port}")
    umask = _int(env, "UMASK", 0o002, base=8)
    if not 0 <= umask <= 0o777:
        raise ConfigError(f"UMASK out of range: {oct(umask)}")
    return Settings(
        config_dir=Path(env.get("PERIODICOOL_CONFIG_DIR", "/config")),
        library_dir=Path(env.get("PERIODICOOL_LIBRARY_DIR", "/library")),
        downloads_dir=Path(env.get("PERIODICOOL_DOWNLOADS_DIR", "/downloads")),
        import_dir=Path(import_dir) if import_dir else None,
        host=env.get("PERIODICOOL_HOST", "0.0.0.0"),
        port=port,
        puid=_int(env, "PUID", 99),
        pgid=_int(env, "PGID", 100),
        umask=umask,
        tz=env.get("TZ", "UTC"),
    )
