"""Download client fields, as in Sonarr: host, port, SSL and URL base (plus each
kind's own options) in a settings column; the address column is kept, composed
from them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-23
"""

import json
import urllib.parse
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def split_url(url: str) -> dict:
    """http://sab:8080/sabnzbd -> host, port, use_ssl, url_base."""
    parts = urllib.parse.urlsplit(url or "")
    ssl = parts.scheme == "https"
    return {
        "host": parts.hostname or "",
        "port": parts.port or (443 if ssl else 80),
        "use_ssl": ssl,
        "url_base": parts.path.rstrip("/"),
    }


def upgrade() -> None:
    with op.batch_alter_table("download_clients") as batch:
        batch.add_column(sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"))
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, url FROM download_clients")).all():
        bind.execute(
            sa.text("UPDATE download_clients SET settings = :s WHERE id = :i"),
            {"s": json.dumps(split_url(row.url)), "i": row.id},
        )


def downgrade() -> None:
    with op.batch_alter_table("download_clients") as batch:
        batch.drop_column("settings")
