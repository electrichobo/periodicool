"""Indexer and download-client lists.

The single Prowlarr and SABnzbd settings become the first rows of two lists, and
releases, grabs and API calls point at the row they came from.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
_OLD_KEYS = (
    "prowlarr.url",
    "prowlarr.backoff_until",
    "prowlarr.backoff_seconds",
    "sabnzbd.url",
    "sabnzbd.category",
    "sabnzbd.path_map",
)


def _indexers_table():
    return sa.table(
        "indexers",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("protocol", sa.String),
        sa.column("url", sa.String),
        sa.column("api_key", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("priority", sa.Integer),
        sa.column("categories", sa.JSON),
        sa.column("settings", sa.JSON),
        sa.column("backoff_until", sa.DateTime(timezone=True)),
        sa.column("backoff_s", sa.Integer),
        sa.column("added_at", sa.DateTime(timezone=True)),
    )


def _clients_table():
    return sa.table(
        "download_clients",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("protocol", sa.String),
        sa.column("url", sa.String),
        sa.column("api_key", sa.String),
        sa.column("username", sa.String),
        sa.column("password", sa.String),
        sa.column("category", sa.String),
        sa.column("path_map", sa.JSON),
        sa.column("enabled", sa.Boolean),
        sa.column("priority", sa.Integer),
        sa.column("added_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    op.create_table(
        "indexers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("protocol", sa.String(10), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("api_key", sa.String(500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backoff_s", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "download_clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("protocol", sa.String(10), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("api_key", sa.String(500), nullable=True),
        sa.Column("username", sa.String(200), nullable=True),
        sa.Column("password", sa.String(500), nullable=True),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("path_map", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("releases") as batch:
        batch.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_releases_source_id", "indexers", ["source_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("api_calls") as batch:
        batch.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_api_calls_source_id", "indexers", ["source_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("grabs") as batch:
        batch.add_column(sa.Column("client_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_grabs_client_id", "download_clients", ["client_id"], ["id"], ondelete="SET NULL"
        )

    # Move the single Prowlarr and SABnzbd settings into the first rows.
    bind = op.get_bind()
    old = {
        k: v
        for k, v in bind.execute(
            sa.select(_settings.c.key, _settings.c.value).where(_settings.c.key.in_(_OLD_KEYS))
        )
    }
    now = datetime.now(UTC)
    if old.get("prowlarr.url"):
        until = old.get("prowlarr.backoff_until")
        bind.execute(
            _indexers_table()
            .insert()
            .values(
                name="Prowlarr",
                kind="prowlarr",
                protocol="usenet",
                url=old["prowlarr.url"],
                api_key=None,  # the PROWLARR_API_KEY environment variable still applies
                enabled=True,
                priority=25,
                categories=[],
                settings={},
                backoff_until=datetime.fromisoformat(until) if until else None,
                backoff_s=int(old.get("prowlarr.backoff_seconds") or 0),
                added_at=now,
            )
        )
        source = bind.execute(sa.text("SELECT id FROM indexers")).scalar_one()
        bind.execute(sa.text("UPDATE releases SET source_id = :s"), {"s": source})
        bind.execute(sa.text("UPDATE api_calls SET source_id = :s"), {"s": source})
    if old.get("sabnzbd.url"):
        bind.execute(
            _clients_table()
            .insert()
            .values(
                name="SABnzbd",
                kind="sabnzbd",
                protocol="usenet",
                url=old["sabnzbd.url"],
                api_key=None,  # the SABNZBD_API_KEY environment variable still applies
                category=old.get("sabnzbd.category") or "",
                path_map=old.get("sabnzbd.path_map") or [],
                enabled=True,
                priority=1,
                added_at=now,
            )
        )
        client = bind.execute(sa.text("SELECT id FROM download_clients")).scalar_one()
        bind.execute(
            sa.text("UPDATE grabs SET client_id = :c WHERE client = 'sabnzbd'"), {"c": client}
        )
    bind.execute(_settings.delete().where(_settings.c.key.in_(_OLD_KEYS)))


def downgrade() -> None:
    bind = op.get_bind()
    rows = []
    first = bind.execute(
        sa.text(
            "SELECT url, backoff_until, backoff_s FROM indexers "
            "WHERE kind = 'prowlarr' ORDER BY priority, id LIMIT 1"
        )
    ).first()
    if first is not None:
        rows.append({"key": "prowlarr.url", "value": first.url})
        if first.backoff_until:
            rows.append({"key": "prowlarr.backoff_until", "value": str(first.backoff_until)})
        rows.append({"key": "prowlarr.backoff_seconds", "value": first.backoff_s})
    client = bind.execute(
        sa.select(_clients_table())
        .where(sa.text("kind = 'sabnzbd'"))
        .order_by(sa.text("priority"), sa.text("id"))
        .limit(1)
    ).first()
    if client is not None:
        rows += [
            {"key": "sabnzbd.url", "value": client.url},
            {"key": "sabnzbd.category", "value": client.category},
            {"key": "sabnzbd.path_map", "value": client.path_map},
        ]
    bind.execute(_settings.delete().where(_settings.c.key.in_(_OLD_KEYS)))
    if rows:
        bind.execute(_settings.insert(), rows)
    with op.batch_alter_table("grabs") as batch:
        batch.drop_constraint("fk_grabs_client_id", type_="foreignkey")
        batch.drop_column("client_id")
    with op.batch_alter_table("api_calls") as batch:
        batch.drop_constraint("fk_api_calls_source_id", type_="foreignkey")
        batch.drop_column("source_id")
    with op.batch_alter_table("releases") as batch:
        batch.drop_constraint("fk_releases_source_id", type_="foreignkey")
        batch.drop_column("source_id")
    op.drop_table("download_clients")
    op.drop_table("indexers")
