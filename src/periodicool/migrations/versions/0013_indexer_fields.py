"""Indexer fields, as in Sonarr: Enable RSS, Enable Automatic Search and Enable
Interactive Search in place of one Enabled switch, a download client per indexer,
and the info hash of blocklisted torrents.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOGGLES = ("enable_rss", "enable_automatic_search", "enable_interactive_search")


def upgrade() -> None:
    with op.batch_alter_table("indexers") as batch:
        for name in TOGGLES:
            batch.add_column(sa.Column(name, sa.Boolean(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("download_client_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_indexers_download_client",
            "download_clients",
            ["download_client_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute("UPDATE indexers SET " + ", ".join(f"{name} = enabled" for name in TOGGLES))
    with op.batch_alter_table("indexers") as batch:
        batch.drop_column("enabled")
    with op.batch_alter_table("blocklist") as batch:
        batch.add_column(sa.Column("info_hash", sa.String(64), nullable=True))
        batch.create_index("ix_blocklist_info_hash", ["info_hash"])


def downgrade() -> None:
    with op.batch_alter_table("blocklist") as batch:
        batch.drop_index("ix_blocklist_info_hash")
        batch.drop_column("info_hash")
    with op.batch_alter_table("indexers") as batch:
        batch.add_column(sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"))
    op.execute("UPDATE indexers SET enabled = (" + " OR ".join(TOGGLES) + ")")
    with op.batch_alter_table("indexers") as batch:
        batch.drop_constraint("fk_indexers_download_client", type_="foreignkey")
        batch.drop_column("download_client_id")
        for name in TOGGLES:
            batch.drop_column(name)
