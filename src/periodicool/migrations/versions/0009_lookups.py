"""Cache for opt-in metadata lookups.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lookup_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("query", sa.String(300), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.UniqueConstraint("source", "query", name="uq_lookup_cache_source_query"),
    )


def downgrade() -> None:
    op.drop_table("lookup_cache")
