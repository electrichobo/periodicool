"""Domain tables: settings, format profiles, magazines and issues."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
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
    # Query sent to Prowlarr; None means the title without punctuation.
    search_term: Mapped[str | None] = mapped_column(String(200))
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
    cover_date: Mapped[date | None] = mapped_column(Date)  # None: number but no date
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
    # Relative to the library root, POSIX separators.
    file_path: Mapped[str | None] = mapped_column(String(1000))

    magazine: Mapped[Magazine] = relationship(back_populates="issues")


class ScanKind(StrEnum):
    LIBRARY = "library"
    IMPORT = "import"
    DOWNLOAD = "download"


class ItemAction(StrEnum):
    MOVE = "move"
    KEEP = "keep"
    REVIEW = "review"


class ItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    GONE = "gone"


class ReasonCode(StrEnum):
    UNPARSED = "unparsed"
    LOW_CONFIDENCE = "low_confidence"
    NO_MAGAZINE = "no_magazine"
    AMBIGUOUS_MAGAZINE = "ambiguous_magazine"
    UNIDENTIFIED = "unidentified"
    DUPLICATE = "duplicate"
    COLLISION = "collision"
    OUTSIDE_ROOT = "outside_root"


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ScanKind] = mapped_column(_enum(ScanKind))
    root: Mapped[str] = mapped_column(String(1000))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict] = mapped_column(JSON, default=dict)


class ImportItem(Base):
    """One source file: its planned action, and its place in the manual import queue."""

    __tablename__ = "import_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"))
    origin: Mapped[ScanKind] = mapped_column(_enum(ScanKind))
    source_path: Mapped[str] = mapped_column(String(1000), unique=True)
    size: Mapped[int]
    mtime: Mapped[float] = mapped_column(Float)
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    magazine_id: Mapped[int | None] = mapped_column(ForeignKey("magazines.id", ondelete="SET NULL"))
    issue_key: Mapped[str | None] = mapped_column(String(100))
    target_path: Mapped[str | None] = mapped_column(String(1000))
    action: Mapped[ItemAction] = mapped_column(_enum(ItemAction))
    reason_code: Mapped[ReasonCode | None] = mapped_column(_enum(ReasonCode))
    reason: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[ItemStatus] = mapped_column(_enum(ItemStatus), default=ItemStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    magazine: Mapped[Magazine | None] = relationship()


class FileMove(Base):
    """Log of every move attempt."""

    __tablename__ = "file_moves"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("import_items.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(1000))
    target: Mapped[str] = mapped_column(String(1000))
    method: Mapped[str | None] = mapped_column(String(10))
    ok: Mapped[bool]
    error: Mapped[str | None] = mapped_column(String(2000))
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReleaseSource(StrEnum):
    RSS = "rss"
    SEARCH = "search"


class Release(Base):
    """A release seen in RSS or search, with its latest accept/reject decision."""

    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    guid: Mapped[str] = mapped_column(String(1000), unique=True)
    indexer_id: Mapped[int | None]
    indexer: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(1000))
    size: Mapped[int | None]
    publish_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    categories: Mapped[list[int]] = mapped_column(JSON, default=list)
    protocol: Mapped[str | None] = mapped_column(String(20))
    info_url: Mapped[str | None] = mapped_column(String(2000))
    # Never holds an API key: it is stripped before saving.
    download_url: Mapped[str | None] = mapped_column(String(2000))
    source: Mapped[ReleaseSource] = mapped_column(_enum(ReleaseSource))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    magazine_id: Mapped[int | None] = mapped_column(ForeignKey("magazines.id", ondelete="SET NULL"))
    issue_key: Mapped[str | None] = mapped_column(String(100))
    accepted: Mapped[bool] = mapped_column(default=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    rank: Mapped[list] = mapped_column(JSON, default=list)

    magazine: Mapped[Magazine | None] = relationship()


class SearchCache(Base):
    __tablename__ = "search_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_key: Mapped[str] = mapped_column(String(500), unique=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    guids: Mapped[list[str]] = mapped_column(JSON, default=list)


class SearchState(Base):
    """Per-magazine backoff for scheduled Wanted searches."""

    __tablename__ = "search_state"

    magazine_id: Mapped[int] = mapped_column(
        ForeignKey("magazines.id", ondelete="CASCADE"), primary_key=True
    )
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    next_search_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(500))


class ApiCall(Base):
    """One request to Prowlarr, for limits, Health and debugging. Never holds secrets."""

    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    indexer_id: Mapped[int | None]
    status: Mapped[int | None]
    ok: Mapped[bool]
    note: Mapped[str | None] = mapped_column(String(1000))


class Job(Base):
    __tablename__ = "jobs"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    interval_s: Mapped[int]
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(2000))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GrabStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    IMPORTED = "imported"
    REVIEW = "review"
    FAILED = "failed"
    REMOVED = "removed"


ACTIVE_GRAB = (GrabStatus.QUEUED, GrabStatus.DOWNLOADING, GrabStatus.COMPLETED)


class Grab(Base):
    """A job PeriodiCool sent to a download client. Only these jobs are ever looked at."""

    __tablename__ = "grabs"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int | None] = mapped_column(ForeignKey("releases.id", ondelete="SET NULL"))
    magazine_id: Mapped[int | None] = mapped_column(ForeignKey("magazines.id", ondelete="SET NULL"))
    issue_key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(1000))
    indexer_id: Mapped[int | None]
    client: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str] = mapped_column(String(100))
    status: Mapped[GrabStatus] = mapped_column(_enum(GrabStatus), default=GrabStatus.QUEUED)
    client_status: Mapped[str | None] = mapped_column(String(50))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    storage_path: Mapped[str | None] = mapped_column(String(2000))
    error: Mapped[str | None] = mapped_column(String(2000))
    import_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_items.id", ondelete="SET NULL")
    )
    grabbed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    release: Mapped[Release | None] = relationship()
    magazine: Mapped[Magazine | None] = relationship()


class BlockedRelease(Base):
    __tablename__ = "blocklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    guid: Mapped[str | None] = mapped_column(String(1000), index=True)
    title: Mapped[str] = mapped_column(String(1000))
    indexer_id: Mapped[int | None]
    magazine_id: Mapped[int | None] = mapped_column(ForeignKey("magazines.id", ondelete="SET NULL"))
    issue_key: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """The one admin. Only a scrypt hash of the password is stored."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginSession(Base):
    """A login. The cookie holds the token; only its sha256 is stored here."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship()
