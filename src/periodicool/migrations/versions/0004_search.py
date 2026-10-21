"""Prowlarr search: releases, search cache, search backoff, API call log, jobs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("magazines") as batch:
        batch.add_column(sa.Column("search_term", sa.String(200), nullable=True))

    op.create_table(
        "releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guid", sa.String(1000), nullable=False, unique=True),
        sa.Column("indexer_id", sa.Integer(), nullable=True),
        sa.Column("indexer", sa.String(200), nullable=True),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("publish_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("protocol", sa.String(20), nullable=True),
        sa.Column("info_url", sa.String(2000), nullable=True),
        sa.Column("download_url", sa.String(2000), nullable=True),
        sa.Column("source", sa.Enum("rss", "search", native_enum=False, length=32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("issue_key", sa.String(100), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("rank", sa.JSON(), nullable=False),
    )
    op.create_table(
        "search_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("query_key", sa.String(500), nullable=False, unique=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guids", sa.JSON(), nullable=False),
    )
    op.create_table(
        "search_state",
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_search_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_search_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.String(500), nullable=True),
    )
    op.create_table(
        "api_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("indexer_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(1000), nullable=True),
    )
    op.create_index("ix_api_calls_at", "api_calls", ["at"])
    op.create_table(
        "jobs",
        sa.Column("name", sa.String(50), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.String(2000), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_index("ix_api_calls_at", table_name="api_calls")
    op.drop_table("api_calls")
    op.drop_table("search_state")
    op.drop_table("search_cache")
    op.drop_table("releases")
    with op.batch_alter_table("magazines") as batch:
        batch.drop_column("search_term")
