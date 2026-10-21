"""Downloads: grabs sent to the download client, and the blocklist.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = ("queued", "downloading", "completed", "imported", "review", "failed", "removed")


def upgrade() -> None:
    op.create_table(
        "grabs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "release_id",
            sa.Integer(),
            sa.ForeignKey("releases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("issue_key", sa.String(100), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("indexer_id", sa.Integer(), nullable=True),
        sa.Column("client", sa.String(20), nullable=False),
        sa.Column("job_id", sa.String(200), nullable=False, unique=True),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("status", sa.Enum(*_STATUS, native_enum=False, length=32), nullable=False),
        sa.Column("client_status", sa.String(50), nullable=True),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("storage_path", sa.String(2000), nullable=True),
        sa.Column("error", sa.String(2000), nullable=True),
        sa.Column(
            "import_item_id",
            sa.Integer(),
            sa.ForeignKey("import_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("grabbed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "blocklist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guid", sa.String(1000), nullable=True),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("indexer_id", sa.Integer(), nullable=True),
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("issue_key", sa.String(100), nullable=True),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_blocklist_guid", "blocklist", ["guid"])


def downgrade() -> None:
    op.drop_index("ix_blocklist_guid", table_name="blocklist")
    op.drop_table("blocklist")
    op.drop_table("grabs")
