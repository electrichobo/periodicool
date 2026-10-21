"""Parity with Sonarr's screens: history, the event log, file details, tags,
covers, and download sizes for the queue.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = (
    "grabbed",
    "download_failed",
    "download_removed",
    "imported",
    "import_failed",
    "import_review",
    "file_moved",
    "file_linked",
    "blocklisted",
)


def upgrade() -> None:
    op.create_table(
        "history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.Enum(*_EVENTS, native_enum=False, length=32), nullable=False),
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("issue_key", sa.String(100), nullable=True),
        sa.Column("source_title", sa.String(1000), nullable=True),
        sa.Column(
            "grab_id",
            sa.Integer(),
            sa.ForeignKey("grabs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_history_date", "history", ["date"])
    op.create_index("ix_history_magazine_id", "history", ["magazine_id"])
    op.create_table(
        "log_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("logger", sa.String(100), nullable=False),
        sa.Column("message", sa.String(4000), nullable=False),
    )
    op.create_index("ix_log_events_time", "log_events", ["time"])
    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("file_size", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("file_format", sa.String(10), nullable=True))
        batch.add_column(sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("magazines") as batch:
        batch.add_column(sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("cover_path", sa.String(1000), nullable=True))
    with op.batch_alter_table("grabs") as batch:
        batch.add_column(sa.Column("size_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("size_left_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("time_left_s", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("grabs") as batch:
        batch.drop_column("time_left_s")
        batch.drop_column("size_left_bytes")
        batch.drop_column("size_bytes")
    with op.batch_alter_table("magazines") as batch:
        batch.drop_column("cover_path")
        batch.drop_column("tags")
    with op.batch_alter_table("issues") as batch:
        batch.drop_column("imported_at")
        batch.drop_column("file_format")
        batch.drop_column("file_size")
    op.drop_index("ix_log_events_time", table_name="log_events")
    op.drop_table("log_events")
    op.drop_index("ix_history_magazine_id", table_name="history")
    op.drop_index("ix_history_date", table_name="history")
    op.drop_table("history")
