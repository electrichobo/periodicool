"""Domain tables: settings, format profiles, magazines, issues.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, length=32)


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    profiles = op.create_table(
        "format_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("formats", sa.JSON(), nullable=False),
        sa.Column("prefer_true_pdf", sa.Boolean(), nullable=False),
        sa.Column("upgrades", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "magazines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("region", sa.String(50), nullable=True),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("filler_optional", sa.Boolean(), nullable=False),
        sa.Column("special_prefix", sa.Boolean(), nullable=False),
        sa.Column(
            "frequency",
            _enum(
                "weekly",
                "biweekly",
                "monthly",
                "thirteen_per_year",
                "bimonthly",
                "quarterly",
                "seasonal",
                "irregular",
            ),
            nullable=False,
        ),
        sa.Column("numbering", _enum("date", "issue", "volume", "season"), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=True),
        sa.Column("anchor_number", sa.Integer(), nullable=True),
        sa.Column("schedule_start", sa.Date(), nullable=True),
        sa.Column("publish_weekday", sa.Integer(), nullable=True),
        sa.Column("grace_days", sa.Integer(), nullable=False),
        sa.Column("monitored", sa.Boolean(), nullable=False),
        sa.Column("monitor_mode", _enum("new_only", "from_year", "all"), nullable=False),
        sa.Column("monitor_from_year", sa.Integer(), nullable=True),
        sa.Column(
            "format_profile_id",
            sa.Integer(),
            sa.ForeignKey("format_profiles.id"),
            nullable=True,
        ),
        sa.Column("must_contain", sa.JSON(), nullable=False),
        sa.Column("must_not_contain", sa.JSON(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "magazine_id",
            sa.Integer(),
            sa.ForeignKey("magazines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("cover_date", sa.Date(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("month", sa.Integer(), nullable=True),
        sa.Column("month_end", sa.Integer(), nullable=True),
        sa.Column("day", sa.Integer(), nullable=True),
        sa.Column("season", sa.String(10), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("volume_number", sa.Integer(), nullable=True),
        sa.Column("special", sa.Boolean(), nullable=False),
        sa.Column("special_title", sa.String(200), nullable=True),
        sa.Column("expected", sa.Boolean(), nullable=False),
        sa.Column("monitored", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            _enum("wanted", "grabbed", "downloaded", "imported", "failed"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.UniqueConstraint("magazine_id", "key", name="uq_issues_magazine_key"),
    )
    op.create_index("ix_issues_magazine_cover_date", "issues", ["magazine_id", "cover_date"])

    op.bulk_insert(
        profiles,
        [
            {
                "name": "Any",
                "formats": ["pdf", "epub", "cbz", "cbr"],
                "prefer_true_pdf": True,
                "upgrades": False,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_issues_magazine_cover_date", table_name="issues")
    op.drop_table("issues")
    op.drop_table("magazines")
    op.drop_table("format_profiles")
    op.drop_table("settings")
