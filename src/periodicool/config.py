"""Environment settings.

Only paths, process settings and secrets come from the environment. The port,
URL base and allowed hosts are edited in the UI (and applied at start); when their
environment variables are set, those win, so a bad saved value can be undone.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
    jobs_enabled: bool = True
    url_base: str = ""
    allowed_hosts: tuple[str, ...] = ()
    # Which of PERIODICOOL_PORT, _URL_BASE and _ALLOWED_HOSTS were set (they beat saved values).
    from_env: frozenset[str] = field(default_factory=frozenset)

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
    from periodicool.web.urlbase import clean_base

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
        jobs_enabled=env.get("PERIODICOOL_JOBS", "on").strip().lower() not in ("off", "0", "false"),
        url_base=clean_base(env.get("PERIODICOOL_URL_BASE", "")),
        allowed_hosts=_hosts(env.get("PERIODICOOL_ALLOWED_HOSTS", "")),
        from_env=frozenset(
            name
            for name in ("PERIODICOOL_PORT", "PERIODICOOL_URL_BASE", "PERIODICOOL_ALLOWED_HOSTS")
            if env.get(name, "").strip()
        ),
    )


def _hosts(raw: str) -> tuple[str, ...]:
    return tuple(h.strip().lower() for h in raw.replace(";", ",").split(",") if h.strip())


PORT_KEY = "server.port"
URL_BASE_KEY = "server.url_base"
HOSTS_KEY = "server.allowed_hosts"


def with_saved_server_settings(settings: Settings) -> Settings:
    """Port, URL base and allowed hosts from the database, unless the environment set them.
    Run at start; changes need a restart."""
    from periodicool import db
    from periodicool.library import get_setting
    from periodicool.web.urlbase import clean_base

    settings.config_dir.mkdir(parents=True, exist_ok=True)
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    try:
        with db.make_session_factory(engine)() as session:
            port = get_setting(session, PORT_KEY)
            base = get_setting(session, URL_BASE_KEY)
            hosts = get_setting(session, HOSTS_KEY)
    finally:
        engine.dispose()
    changes: dict = {}
    if "PERIODICOOL_PORT" not in settings.from_env and isinstance(port, int) and 1 <= port <= 65535:
        changes["port"] = port
    if "PERIODICOOL_URL_BASE" not in settings.from_env and base:
        changes["url_base"] = clean_base(base)
    if "PERIODICOOL_ALLOWED_HOSTS" not in settings.from_env and hosts:
        changes["allowed_hosts"] = tuple(str(h).strip().lower() for h in hosts if str(h).strip())
    return replace(settings, **changes) if changes else settings
