"""Library scans, import items (plan and manual import queue), move log.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = ("library", "import")


def _enum(*values: str) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, length=32)


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.alter_column("cover_date", existing_type=sa.Date(), nullable=True)

    op.create_table(
        "scans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", _enum(*_KIND), nullable=False),
        sa.Column("root", sa.String(1000), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
    )
    op.create_table(
        "import_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_id", sa.Integer(), sa.ForeignKey("scans.id"), nullable=False),
        sa.Column("origin", _enum(*_KIND), nullable=False),
        sa.Column("source_path", sa.String(1000), nullable=False, unique=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("parsed", sa.JSON(), nullable=False),
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("issue_key", sa.String(100), nullable=True),
        sa.Column("target_path", sa.String(1000), nullable=True),
        sa.Column("action", _enum("move", "keep", "review"), nullable=False),
        sa.Column(
            "reason_code",
            _enum(
                "unparsed",
                "low_confidence",
                "no_magazine",
                "ambiguous_magazine",
                "unidentified",
                "duplicate",
                "collision",
                "outside_root",
            ),
            nullable=True,
        ),
        sa.Column("reason", sa.String(2000), nullable=True),
        sa.Column("status", _enum("pending", "done", "skipped", "failed", "gone"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "file_moves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("import_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(1000), nullable=False),
        sa.Column("target", sa.String(1000), nullable=False),
        sa.Column("method", sa.String(10), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(2000), nullable=True),
        sa.Column("moved_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("file_moves")
    op.drop_table("import_items")
    op.drop_table("scans")
    # Issues without a cover date cannot go back to a NOT NULL column. Give them a
    # placeholder date instead of deleting any rows.
    op.execute("UPDATE issues SET cover_date = '1900-01-01' WHERE cover_date IS NULL")
    with op.batch_alter_table("issues") as batch:
        batch.alter_column("cover_date", existing_type=sa.Date(), nullable=False)
