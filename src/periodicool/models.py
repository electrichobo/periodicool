"""Domain tables: settings, format profiles, magazines and issues."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from periodicool.db import Base
from periodicool.matching import TitleSpec


class Frequency(StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    THIRTEEN_PER_YEAR = "thirteen_per_year"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"
    SEASONAL = "seasonal"
    IRREGULAR = "irregular"


class Numbering(StrEnum):
    DATE = "date"
    ISSUE = "issue"
    VOLUME = "volume"
    SEASON = "season"


class MonitorMode(StrEnum):
    NEW_ONLY = "new_only"
    FROM_YEAR = "from_year"
    ALL = "all"


class IssueStatus(StrEnum):
    WANTED = "wanted"
    GRABBED = "grabbed"
    DOWNLOADED = "downloaded"
    IMPORTED = "imported"
    FAILED = "failed"


def _enum(cls: type[StrEnum]) -> Enum:
    return Enum(
        cls,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)


class FormatProfile(Base):
    __tablename__ = "format_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Best first, from pdf, epub, cbz, cbr.
    formats: Mapped[list[str]] = mapped_column(JSON)
    prefer_true_pdf: Mapped[bool] = mapped_column(default=True)
    upgrades: Mapped[bool] = mapped_column(default=False)


class Magazine(Base):
    __tablename__ = "magazines"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    region: Mapped[str | None] = mapped_column(String(50))
    languages: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["english"])
    filler_optional: Mapped[bool] = mapped_column(default=True)
    special_prefix: Mapped[bool] = mapped_column(default=True)

    frequency: Mapped[Frequency] = mapped_column(_enum(Frequency))
    numbering: Mapped[Numbering] = mapped_column(_enum(Numbering))
    # A known issue the schedule counts from, e.g. Fortean Times 437 = Nov 2023.
    anchor_date: Mapped[date | None] = mapped_column(Date)
    anchor_number: Mapped[int | None]
    # When the current cadence began; nothing is generated before it.
    schedule_start: Mapped[date | None] = mapped_column(Date)
    publish_weekday: Mapped[int | None]  # 0 = Monday, for weeklies
    grace_days: Mapped[int] = mapped_column(default=14)

    monitored: Mapped[bool] = mapped_column(default=True)
    monitor_mode: Mapped[MonitorMode] = mapped_column(
        _enum(MonitorMode), default=MonitorMode.NEW_ONLY
    )
    monitor_from_year: Mapped[int | None]

    format_profile_id: Mapped[int | None] = mapped_column(ForeignKey("format_profiles.id"))
    must_contain: Mapped[list[str]] = mapped_column(JSON, default=list)
    must_not_contain: Mapped[list[str]] = mapped_column(JSON, default=list)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    format_profile: Mapped[FormatProfile | None] = relationship()
    issues: Mapped[list[Issue]] = relationship(
        back_populates="magazine", cascade="all, delete-orphan", passive_deletes=True
    )

    def title_spec(self) -> TitleSpec:
        return TitleSpec(
            title=self.title,
            aliases=tuple(self.aliases or ()),
            region=self.region,
            languages=tuple(self.languages or ("english",)),
            filler_optional=True if self.filler_optional is None else self.filler_optional,
            special_prefix=True if self.special_prefix is None else self.special_prefix,
        )


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("magazine_id", "key", name="uq_issues_magazine_key"),
        Index("ix_issues_magazine_cover_date", "magazine_id", "cover_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    magazine_id: Mapped[int] = mapped_column(ForeignKey("magazines.id", ondelete="CASCADE"))
    # Canonical identity: 2023-11, 2023-07-10, 2013-autumn, n437, v41, special-2023-07-07.
    key: Mapped[str] = mapped_column(String(100))
    cover_date: Mapped[date] = mapped_column(Date)
    year: Mapped[int | None]
    month: Mapped[int | None]
    month_end: Mapped[int | None]
    day: Mapped[int | None]
    season: Mapped[str | None] = mapped_column(String(10))
    issue_number: Mapped[int | None]
    volume_number: Mapped[int | None]
    special: Mapped[bool] = mapped_column(default=False)
    special_title: Mapped[str | None] = mapped_column(String(200))
    expected: Mapped[bool] = mapped_column(default=True)
    monitored: Mapped[bool] = mapped_column(default=True)
    status: Mapped[IssueStatus] = mapped_column(_enum(IssueStatus), default=IssueStatus.WANTED)
    file_path: Mapped[str | None] = mapped_column(String(1000))

    magazine: Mapped[Magazine] = relationship(back_populates="issues")
