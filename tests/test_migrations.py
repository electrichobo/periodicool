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
