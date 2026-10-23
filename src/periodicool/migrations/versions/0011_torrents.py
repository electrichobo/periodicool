"""Torrents: seeders on releases, the protocol of each grab, and imports that keep
their source (a torrent goes on seeding from the client's folder).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("releases") as batch:
        batch.add_column(sa.Column("seeders", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("peers", sa.Integer(), nullable=True))
    with op.batch_alter_table("grabs") as batch:
        batch.add_column(
            sa.Column("protocol", sa.String(10), nullable=False, server_default="usenet")
        )
    with op.batch_alter_table("import_items") as batch:
        batch.add_column(
            sa.Column("keep_source", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("import_items") as batch:
        batch.drop_column("keep_source")
    with op.batch_alter_table("grabs") as batch:
        batch.drop_column("protocol")
    with op.batch_alter_table("releases") as batch:
        batch.drop_column("peers")
        batch.drop_column("seeders")
