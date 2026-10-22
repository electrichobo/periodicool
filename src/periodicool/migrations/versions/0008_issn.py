"""ISSN and on-sale lead time on magazines.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("magazines") as batch:
        batch.add_column(sa.Column("issn", sa.String(9), nullable=True))
        batch.add_column(sa.Column("on_sale_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("magazines") as batch:
        batch.drop_column("on_sale_days")
        batch.drop_column("issn")
