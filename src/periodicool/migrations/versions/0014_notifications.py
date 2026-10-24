"""Notifications: Sonarr's Webhook connections under Settings, Connect.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="webhook"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("username", sa.String(200), nullable=True),
        sa.Column("password", sa.String(500), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("events", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("last_status", sa.String(20), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notifications")
