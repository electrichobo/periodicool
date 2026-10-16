"""Alembic environment.

The app runs migrations on startup through `periodicool.db.upgrade`. For the CLI
(`alembic revision ...`), the database URL comes from the environment settings.
"""

from alembic import context

import periodicool.models  # noqa: F401  (registers tables on Base.metadata)
from periodicool.config import load_settings
from periodicool.db import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or load_settings().db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Foreign keys stay off while migrating: SQLite batch operations rebuild a
    # table by dropping it, which would otherwise cascade-delete child rows
    # (dropping a magazines column would wipe issues). Checked afterwards.
    engine = make_engine(_url(), foreign_keys=False)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        problems = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key check failed after migration: {problems[:5]}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
