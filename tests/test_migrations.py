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
