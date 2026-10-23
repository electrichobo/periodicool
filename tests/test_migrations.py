from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from periodicool import db


def _current_revision(url: str) -> str:
    engine = create_engine(url)
    with engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    return rev


def test_upgrade_reaches_head(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    head = ScriptDirectory.from_config(db.alembic_config(url)).get_current_head()
    assert _current_revision(url) == head


def test_upgrade_is_idempotent(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    db.upgrade(url)


def test_single_head():
    script = ScriptDirectory.from_config(db.alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_models_match_migrations(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import periodicool.models  # noqa: F401

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = db.make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.Base.metadata)
    engine.dispose()
    assert diff == []


def test_default_format_profile_seeded(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name, formats FROM format_profiles")).all()
    engine.dispose()
    assert rows == [("Any", '["pdf", "epub", "cbz", "cbr"]')]


def test_downgrade_and_upgrade_again(tmp_path):
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = db.alembic_config(url)
    db.upgrade(url)
    command.downgrade(cfg, "0001")
    assert _current_revision(url) == "0001"
    db.upgrade(url)
    assert _current_revision(url) == ScriptDirectory.from_config(cfg).get_current_head()


def test_downgrade_keeps_undated_issues(tmp_path):
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO magazines (title, aliases, languages, filler_optional, special_prefix,"
                " frequency, numbering, grace_days, monitored, monitor_mode, must_contain,"
                " must_not_contain, added_at) VALUES ('Make:', '[]', '[]', 1, 1, 'irregular',"
                " 'volume', 14, 1, 'new_only', '[]', '[]', '2026-10-16')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO issues (magazine_id, key, special, expected, monitored, status)"
                " VALUES (1, 'v5', 0, 0, 1, 'imported')"
            )
        )
    engine.dispose()
    command.downgrade(db.alembic_config(url), "0002")
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT key, cover_date FROM issues")).all()
    engine.dispose()
    assert rows == [("v5", "1900-01-01")]


def test_single_prowlarr_and_sab_become_list_rows(tmp_path):
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = db.alembic_config(url)
    command.upgrade(cfg, "0009")
    engine = create_engine(url)
    with engine.begin() as conn:
        for key, value in [
            ("prowlarr.url", '"http://prowlarr:9696"'),
            ("prowlarr.backoff_seconds", "300"),
            ("sabnzbd.url", '"http://sab:8080"'),
            ("sabnzbd.category", '"mags"'),
            ("sabnzbd.path_map", '[["/data/complete/mags", "/downloads"]]'),
            ("grab.auto", "false"),
        ]:
            conn.execute(
                text("INSERT INTO settings (key, value) VALUES (:k, :v)"), {"k": key, "v": value}
            )
        conn.execute(
            text(
                "INSERT INTO releases (guid, title, categories, source, first_seen_at,"
                " last_seen_at, accepted, reasons, rank)"
                " VALUES ('g1', 'T', '[]', 'rss', '2026-10-01', '2026-10-01', 0, '[]', '[]')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO grabs (issue_key, title, client, job_id, category, status, progress,"
                " grabbed_at, updated_at) VALUES ('n1', 'T', 'sabnzbd', 'J1', 'mags', 'queued', 0,"
                " '2026-10-01', '2026-10-01')"
            )
        )
        conn.execute(
            text("INSERT INTO api_calls (at, kind, ok) VALUES ('2026-10-01', 'search', 1)")
        )
    engine.dispose()

    db.upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        ix = conn.execute(
            text("SELECT id, name, kind, url, api_key, backoff_s FROM indexers")
        ).all()
        cl = conn.execute(
            text("SELECT id, name, kind, url, category, path_map FROM download_clients")
        ).all()
        assert [(r.name, r.kind, r.url, r.api_key, r.backoff_s) for r in ix] == [
            ("Prowlarr", "prowlarr", "http://prowlarr:9696", None, 300)
        ]
        assert [(r.name, r.kind, r.url, r.category) for r in cl] == [
            ("SABnzbd", "sabnzbd", "http://sab:8080", "mags")
        ]
        assert "/downloads" in cl[0].path_map
        assert conn.execute(text("SELECT source_id FROM releases")).scalar() == ix[0].id
        assert conn.execute(text("SELECT source_id FROM api_calls")).scalar() == ix[0].id
        assert conn.execute(text("SELECT client_id FROM grabs")).scalar() == cl[0].id
        keys = set(conn.execute(text("SELECT key FROM settings")).scalars())
        assert "prowlarr.url" not in keys and "sabnzbd.url" not in keys and "grab.auto" in keys
    engine.dispose()

    command.downgrade(cfg, "0009")
    engine = create_engine(url)
    with engine.connect() as conn:
        restored = dict(conn.execute(text("SELECT key, value FROM settings")).all())
    engine.dispose()
    assert restored["prowlarr.url"] == '"http://prowlarr:9696"'
    assert restored["sabnzbd.category"] == '"mags"'


def test_fresh_install_has_empty_lists(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM indexers")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM download_clients")).scalar() == 0
    engine.dispose()


def test_torrent_columns_and_existing_grabs(tmp_path):
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = db.alembic_config(url)
    command.upgrade(cfg, "0010")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO grabs (issue_key, title, client, job_id, category, status, progress,"
                " grabbed_at, updated_at) VALUES ('n1', 'T', 'sabnzbd', 'J1', 'mags', 'queued', 0,"
                " '2026-10-01', '2026-10-01')"
            )
        )
    engine.dispose()
    db.upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT protocol FROM grabs")).scalar() == "usenet"
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(releases)"))}
        assert {"seeders", "peers"} <= cols
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(import_items)"))}
        assert "keep_source" in cols
    engine.dispose()
    command.downgrade(cfg, "0010")
    engine = create_engine(url)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(grabs)"))}
        assert "protocol" not in cols
        assert conn.execute(text("SELECT job_id FROM grabs")).scalar() == "J1"
    engine.dispose()


def test_client_address_split_into_fields(tmp_path):
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = db.alembic_config(url)
    command.upgrade(cfg, "0011")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO download_clients (name, kind, protocol, url, category, path_map,"
                " enabled, priority, added_at) VALUES ('SABnzbd', 'sabnzbd', 'usenet',"
                " 'http://sab:8080/sabnzbd', 'mags', '[]', 1, 1, '2026-10-01')"
            )
        )
    engine.dispose()
    db.upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT url, settings FROM download_clients")).one()
    engine.dispose()
    import json

    assert json.loads(row.settings) == {
        "host": "sab",
        "port": 8080,
        "use_ssl": False,
        "url_base": "/sabnzbd",
    }
    from periodicool.clients.registry import compose_url

    assert compose_url(json.loads(row.settings)) == row.url
    command.downgrade(cfg, "0011")


def test_indexer_enabled_becomes_three_toggles(tmp_path):
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = db.alembic_config(url)
    command.upgrade(cfg, "0012")
    engine = create_engine(url)
    with engine.begin() as conn:
        for name, enabled in (("On", 1), ("Off", 0)):
            conn.execute(
                text(
                    "INSERT INTO indexers (name, kind, protocol, url, enabled, priority,"
                    " categories, settings, backoff_s, added_at) VALUES (:n, 'newznab',"
                    " 'usenet', 'https://nz.example', :e, 25, '[]', '{}', 0, '2026-10-01')"
                ),
                {"n": name, "e": enabled},
            )
    engine.dispose()
    db.upgrade(url)
    engine = db.make_engine(url)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT name, enable_rss, enable_automatic_search, enable_interactive_search,"
                " download_client_id FROM indexers ORDER BY name"
            )
        ).all()
        assert [tuple(r) for r in rows] == [("Off", 0, 0, 0, None), ("On", 1, 1, 1, None)]
        conn.execute(
            text(
                "INSERT INTO download_clients (id, name, kind, protocol, url, category,"
                " path_map, settings, enabled, priority, added_at) VALUES (7, 'SAB', 'sabnzbd',"
                " 'usenet', 'http://sab:8080', 'mags', '[]', '{}', 1, 1, '2026-10-01')"
            )
        )
        conn.execute(text("UPDATE indexers SET download_client_id = 7"))
        conn.execute(text("DELETE FROM download_clients WHERE id = 7"))
        assert conn.execute(text("SELECT download_client_id FROM indexers")).scalars().all() == [
            None,
            None,
        ]
        conn.execute(text("SELECT info_hash FROM blocklist"))
    engine.dispose()
    command.downgrade(cfg, "0012")
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name, enabled FROM indexers ORDER BY name")).all()
    engine.dispose()
    assert [tuple(r) for r in rows] == [("Off", 0), ("On", 1)]
