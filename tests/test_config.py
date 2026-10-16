from pathlib import Path

import pytest

from periodicool.config import ConfigError, load_settings


def test_defaults_match_container_layout():
    s = load_settings({})
    assert s.config_dir == Path("/config")
    assert s.library_dir == Path("/library")
    assert s.downloads_dir == Path("/downloads")
    assert s.import_dir is None
    assert (s.puid, s.pgid) == (99, 100)
    assert s.umask == 0o002
    assert s.port == 8383
    assert s.tz == "UTC"


def test_overrides():
    s = load_settings(
        {
            "PERIODICOOL_IMPORT_DIR": "/import",
            "PERIODICOOL_PORT": "9000",
            "PUID": "1000",
            "PGID": "1000",
            "UMASK": "022",
            "TZ": "Europe/London",
        }
    )
    assert s.import_dir == Path("/import")
    assert s.port == 9000
    assert (s.puid, s.pgid) == (1000, 1000)
    assert s.umask == 0o022
    assert s.tz == "Europe/London"


def test_db_url_uses_forward_slashes():
    s = load_settings({"PERIODICOOL_CONFIG_DIR": "/config"})
    assert s.db_url == "sqlite:////config/periodicool.db"


@pytest.mark.parametrize(
    "env",
    [
        {"PERIODICOOL_PORT": "abc"},
        {"PERIODICOOL_PORT": "70000"},
        {"UMASK": "9"},
        {"UMASK": "1000"},
        {"PUID": "x"},
    ],
)
def test_invalid_values_raise(env):
    with pytest.raises(ConfigError):
        load_settings(env)
