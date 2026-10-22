"""RSS sync, magazine searches and indexer limits, across the indexer list.

Each enabled indexer entry is searched in priority order: a Prowlarr entry fans
out to its own indexers (whose query and grab limits Prowlarr enforces), and a
direct Newznab entry is one indexer with an optional daily cap. PeriodiCool leaves
out indexers that are disabled or at their limit, backs off per entry when told
to slow down, spaces its own requests, caches search results per entry, backs off
per magazine when searches find nothing new, and never searches one magazine
twice in a run. A PeriodiCool-wide daily cap exists only if the user sets one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from periodicool.decisions import Evaluation, evaluate, load_options
from periodicool.library import get_setting
from periodicool.models import (
    ApiCall,
    Frequency,
    Indexer,
    Issue,
    IssueStatus,
    Magazine,
    Release,
    ReleaseSource,
    SearchCache,
    SearchState,
)
from periodicool.prowlarr import (
    FoundRelease,
    IndexerInfo,
    ProwlarrClient,
    ProwlarrError,
    RateLimited,
)
from periodicool.schedule import sync_expected_issues

log = logging.getLogger(__name__)

API_KEY_ENV = "PROWLARR_API_KEY"
CATEGORIES_KEY = "search.categories"
CACHE_HOURS_KEY = "search.cache_hours"
DAILY_CAP_KEY = "search.daily_cap"
MIN_INTERVAL_KEY = "search.min_interval_s"

DEFAULT_CATEGORIES = [7010]
FIRST_BACKOFF = timedelta(minutes=5)
MAX_BACKOFF = timedelta(hours=6)
SEARCH_BACKOFF = timedelta(hours=6)
MAX_SEARCH_BACKOFF = timedelta(days=7)
API_LOG_DAYS = 30


class Unavailable(ProwlarrError):
    """Not searching right now: backoff, daily cap, or no indexer with limit left."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything here is UTC."""
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def make_client(session: Session, transport=None, api_key: str | None = None):
    """The first enabled Prowlarr entry as a client, or None. `api_key` overrides the
    environment fallback (tests)."""
    from periodicool.sources import PROWLARR_KEY_ENV, build_indexer

    row = session.scalars(
        select(Indexer)
        .where(Indexer.enabled.is_(True), Indexer.kind == "prowlarr")
        .order_by(Indexer.priority, Indexer.id)
    ).first()
    if row is None:
        return None
    env = {PROWLARR_KEY_ENV: api_key} if api_key else None
    return build_indexer(session, row, {"prowlarr": transport}, env)


def _clients(source) -> list:
    """A Sources object gives every enabled indexer entry; a single client is itself."""
    from periodicool.sources import Sources

    if isinstance(source, Sources):
        return source.indexers
    return [] if source is None else [source]


def _name(client) -> str:
    return getattr(client, "source_name", None) or "Prowlarr"


def default_search_term(mag: Magazine) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", mag.title)).strip()


def _categories(session: Session) -> list[int]:
    return list(get_setting(session, CATEGORIES_KEY, DEFAULT_CATEGORIES))


# Backoff per indexer entry


def _backoff_reason(client, now: datetime) -> str | None:
    row = getattr(client, "row", None)
    until = _aware(row.backoff_until) if row is not None else None
    if until and now < until:
        return f"{_name(client)} asked us to slow down; waiting until {until:%Y-%m-%d %H:%M} UTC"
    return None


def check_backoff(session: Session, now: datetime, client=None) -> None:
    if client is not None and (why := _backoff_reason(client, now)):
        raise Unavailable(why)


def _rate_limited(session: Session, now: datetime, client) -> datetime:
    row = getattr(client, "row", None)
    step = timedelta(seconds=(row.backoff_s if row is not None else 0) or 0)
    step = min(max(FIRST_BACKOFF, step * 2), MAX_BACKOFF)
    until = now + step
    if row is not None:
        row.backoff_s, row.backoff_until = int(step.total_seconds()), until
    log.warning("%s rate limited us; backing off until %s", _name(client), until)
    return until


def _clear_backoff(session: Session, client) -> None:
    row = getattr(client, "row", None)
    if row is not None and (row.backoff_s or row.backoff_until):
        row.backoff_s, row.backoff_until = 0, None


# Indexers and limits


@dataclass
class IndexerLimit:
    id: int
    name: str
    query_limit: int | None
    grab_limit: int | None
    queries_today: int = 0
    grabs_today: int = 0
    disabled_until: datetime | None = None

    @property
    def queries_left(self) -> int | None:
        return None if self.query_limit is None else max(0, self.query_limit - self.queries_today)

    @property
    def grabs_left(self) -> int | None:
        return None if self.grab_limit is None else max(0, self.grab_limit - self.grabs_today)

    def unavailable(self, now: datetime) -> str | None:
        if self.disabled_until and self.disabled_until > now:
            return f"disabled by Prowlarr until {self.disabled_until:%Y-%m-%d %H:%M}"
        if self.queries_left == 0:
            return f"query limit reached ({self.query_limit} today)"
        return None


def indexer_limits(
    session: Session, client: ProwlarrClient, now: datetime | None = None, category: int = 7010
) -> list[IndexerLimit]:
    """Usenet indexers that carry magazines, with today's use against their limits."""
    now = now or _now()
    if getattr(client, "kind", "") == "newznab":
        return [_newznab_limit(session, client, now)]
    infos: list[IndexerInfo] = [
        i
        for i in client.indexers(category)
        if i.enabled and i.protocol == "usenet" and i.supports_category
    ]
    status = client.indexer_status()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    usage = client.indexer_stats(start) if any(i.query_limit or i.grab_limit for i in infos) else {}
    out = []
    for i in infos:
        u = usage.get(i.id, {})
        out.append(
            IndexerLimit(
                id=i.id,
                name=i.name,
                query_limit=i.query_limit,
                grab_limit=i.grab_limit,
                # Counting RSS as queries too errs on the safe side.
                queries_today=u.get("queries", 0) + u.get("rss", 0),
                grabs_today=u.get("grabs", 0),
                disabled_until=_aware(status.get(i.id)),
            )
        )
    return out


def _newznab_limit(session: Session, client, now: datetime) -> IndexerLimit:
    """A direct Newznab entry is one indexer; its caps are the entry's own settings."""
    row = getattr(client, "row", None)
    settings = (row.settings if row is not None else None) or {}
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def count(kinds: tuple[str, ...]) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(ApiCall)
                .where(
                    ApiCall.source_id == client.source_id,
                    ApiCall.kind.in_(kinds),
                    ApiCall.at >= start,
                )
            )
            or 0
        )

    def cap(name: str) -> int | None:
        value = settings.get(name)
        return int(value) if isinstance(value, int | float) and value > 0 else None

    return IndexerLimit(
        id=0,
        name=_name(client),
        query_limit=cap("daily_cap"),
        grab_limit=cap("grab_cap"),
        queries_today=count(("search", "rss")),
        grabs_today=count(("grab",)),
    )


def _usable(session, client, now) -> tuple[list[IndexerLimit], dict[str, str]]:
    usable, skipped = [], {}
    for lim in indexer_limits(session, client, now, _categories(session)[0]):
        if why := lim.unavailable(now):
            skipped[lim.name] = why
        else:
            usable.append(lim)
    return usable, skipped


# Releases


def _upsert(
    session: Session,
    f: FoundRelease,
    source: ReleaseSource,
    ev: Evaluation,
    now: datetime,
    source_id: int | None = None,
) -> tuple[Release, bool]:
    rel = session.scalars(select(Release).where(Release.guid == f.guid)).one_or_none()
    new = rel is None
    if new:
        rel = Release(guid=f.guid, source=source, first_seen_at=now)
        session.add(rel)
    rel.indexer_id, rel.indexer, rel.title = f.indexer_id, f.indexer, f.title
    rel.size, rel.publish_date, rel.protocol = f.size, f.publish_date, f.protocol
    rel.categories = list(f.categories)
    rel.download_url, rel.info_url = f.download_url, f.info_url
    rel.source_id = source_id
    rel.last_seen_at = now
    _apply_evaluation(rel, ev)
    return rel, new


def _apply_evaluation(rel: Release, ev: Evaluation) -> None:
    rel.magazine_id = ev.magazine.id if ev.magazine else None
    rel.issue_key, rel.accepted = ev.issue_key, ev.accepted
    # Rejections, then notes that flag an accepted release (an estimated month).
    rel.reasons = list(ev.reasons) + [f"note: {n}" for n in ev.notes]
    rel.rank = list(ev.rank)


def _evaluate(session, title, size, protocol, published, magazines, today, options, guid=None):
    return evaluate(
        session,
        title,
        magazines,
        today,
        size=size,
        protocol=protocol,
        published=_aware(published),
        options=options,
        guid=guid,
    )


@dataclass
class SyncResult:
    seen: int = 0
    new: int = 0
    accepted: list[Release] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def rss_sync(session: Session, source, today: date, now: datetime | None = None) -> SyncResult:
    """New releases from every enabled indexer entry's RSS feeds."""
    now = now or _now()
    session.execute(delete(ApiCall).where(ApiCall.at < now - timedelta(days=API_LOG_DAYS)))
    result = SyncResult()
    magazines = list(session.scalars(select(Magazine)))
    options = load_options(session)
    cats = _categories(session)
    for client in _clients(source):
        if why := _backoff_reason(client, now):
            result.skipped[_name(client)] = why
            continue
        try:
            usable, skipped = _usable(session, client, now)
        except RateLimited:
            _rate_limited(session, now, client)
            result.errors.append(f"{_name(client)}: rate limited")
            continue
        except ProwlarrError as exc:
            result.errors.append(f"{_name(client)}: {exc}")
            continue
        result.skipped.update(skipped)
        limited = False
        for lim in usable:
            try:
                found, _limits = client.rss(lim.id, cats)
                found = [replace(f, indexer=lim.name) for f in found]
            except RateLimited:
                _rate_limited(session, now, client)
                result.errors.append(f"{lim.name}: rate limited")
                limited = True
                break
            except ProwlarrError as exc:
                result.errors.append(f"{lim.name}: {exc}")
                continue
            for f in found:
                ev = _evaluate(
                    session,
                    f.title,
                    f.size,
                    f.protocol,
                    f.publish_date,
                    magazines,
                    today,
                    options,
                    f.guid,
                )
                rel, new = _upsert(session, f, ReleaseSource.RSS, ev, now, client.source_id)
                result.seen += 1
                result.new += new
                if rel.accepted:
                    result.accepted.append(rel)
        if not limited:
            _clear_backoff(session, client)
    session.flush()
    return result


# Searches


@dataclass
class SearchResult:
    magazine: Magazine
    candidates: list[tuple[Release, Evaluation]] = field(default_factory=list)
    from_cache: bool = False
    new_accepted: int = 0
    skipped: dict[str, str] = field(default_factory=dict)


def _searches_today(session: Session, now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(
        select(func.count())
        .select_from(ApiCall)
        .where(ApiCall.kind == "search", ApiCall.at >= start)
    )


def search_magazine(
    session: Session,
    source,
    mag: Magazine,
    today: date,
    *,
    force: bool = False,
    scheduled: bool = False,
    now: datetime | None = None,
) -> SearchResult:
    """Search one magazine on every enabled indexer entry. One search covers all of
    its issues. Raises Unavailable when no entry could search and nothing was cached."""
    now = now or _now()
    result = SearchResult(magazine=mag)
    magazines = list(session.scalars(select(Magazine)))
    options = load_options(session)
    cats = _categories(session)
    terms = list(dict.fromkeys([mag.search_term or default_search_term(mag), *(mag.aliases or [])]))
    cache_age = timedelta(hours=get_setting(session, CACHE_HOURS_KEY, 6))
    clients = _clients(source)
    if not clients:
        raise Unavailable("no indexer is set up (Settings, Indexers)")
    problems: list[str] = []
    answered = False
    seen_ids: set[int] = set()

    def add(rel: Release, ev: Evaluation) -> None:
        if rel.id not in seen_ids:
            seen_ids.add(rel.id)
            result.candidates.append((rel, ev))

    for term in terms:
        for client in clients:
            qkey = f"{client.source_id or 0}|{term.lower()}|{','.join(map(str, cats))}"
            cache = session.scalars(
                select(SearchCache).where(SearchCache.query_key == qkey)
            ).one_or_none()
            if cache and not force and _aware(cache.fetched_at) > now - cache_age:
                result.from_cache = answered = True
                for rel in session.scalars(select(Release).where(Release.guid.in_(cache.guids))):
                    ev = _evaluate(
                        session,
                        rel.title,
                        rel.size,
                        rel.protocol,
                        rel.publish_date,
                        magazines,
                        today,
                        options,
                        rel.guid,
                    )
                    _apply_evaluation(rel, ev)
                    session.flush()
                    add(rel, ev)
                continue
            if why := _backoff_reason(client, now):
                result.skipped[_name(client)] = why
                problems.append(why)
                continue
            cap = get_setting(session, DAILY_CAP_KEY)
            if cap is not None and _searches_today(session, now) >= cap:
                raise Unavailable(f"daily search cap of {cap} reached")
            try:
                usable, skipped = _usable(session, client, now)
                result.skipped.update(skipped)
                if not usable:
                    problems.append(
                        f"{_name(client)}: no indexer available"
                        + (
                            ": " + "; ".join(f"{k}: {v}" for k, v in skipped.items())
                            if skipped
                            else ""
                        )
                    )
                    continue
                found = client.search(term, cats, [u.id for u in usable])
            except RateLimited:
                _rate_limited(session, now, client)
                problems.append(f"{_name(client)}: rate limited")
                continue
            except ProwlarrError as exc:
                problems.append(f"{_name(client)}: {exc}")
                continue
            _clear_backoff(session, client)
            answered = True
            guids = []
            for f in found:
                ev = _evaluate(
                    session,
                    f.title,
                    f.size,
                    f.protocol,
                    f.publish_date,
                    magazines,
                    today,
                    options,
                    f.guid,
                )
                rel, new = _upsert(session, f, ReleaseSource.SEARCH, ev, now, client.source_id)
                session.flush()
                guids.append(f.guid)
                add(rel, ev)
                if new and rel.accepted:
                    result.new_accepted += 1
            if cache is None:
                cache = SearchCache(query_key=qkey)
                session.add(cache)
            cache.fetched_at, cache.guids = now, list(dict.fromkeys(guids))

    if not answered:
        raise Unavailable("; ".join(dict.fromkeys(problems)) or "no indexer could search")
    for problem in problems:
        log.info("Search for %s: %s", mag.title, problem)
    if scheduled:
        _update_backoff(session, mag, now, result)
    session.flush()
    return result


def _update_backoff(session: Session, mag: Magazine, now: datetime, result: SearchResult) -> None:
    state = session.get(SearchState, mag.id) or SearchState(magazine_id=mag.id, attempts=0)
    session.add(state)
    state.last_search_at = now
    if result.new_accepted:
        state.attempts = 0
        wait = SEARCH_BACKOFF
    else:
        state.attempts += 1
        wait = min(SEARCH_BACKOFF * 2 ** (state.attempts - 1), MAX_SEARCH_BACKOFF)
    state.next_search_at = now + wait
    accepted = sum(1 for r, _ in result.candidates if r.accepted)
    state.last_result = (
        f"{len(result.candidates)} candidates, {accepted} accepted, {result.new_accepted} new"
        + (" (cache)" if result.from_cache else "")
    )


def wanted_issues(session: Session, mag: Magazine, today: date) -> list[Issue]:
    early = load_options(session).early_days
    if mag.on_sale_days is not None:
        early = mag.on_sale_days  # searchable from the usual on-sale date
    return list(
        session.scalars(
            select(Issue)
            .where(
                Issue.magazine_id == mag.id,
                Issue.monitored.is_(True),
                Issue.file_path.is_(None),
                Issue.status.in_([IssueStatus.WANTED, IssueStatus.FAILED]),
                or_(Issue.cover_date.is_(None), Issue.cover_date <= today + timedelta(days=early)),
            )
            .order_by(Issue.cover_date)
        )
    )


def wanted_search(
    session: Session,
    client,
    today: date,
    *,
    max_magazines: int = 5,
    now: datetime | None = None,
) -> list[SearchResult]:
    """Search magazines with wanted issues whose backoff has passed, oldest first."""
    now = now or _now()
    due = []
    for mag in session.scalars(select(Magazine).where(Magazine.monitored.is_(True))):
        sync_expected_issues(session, mag, today)
        if not wanted_issues(session, mag, today) and mag.frequency is not Frequency.IRREGULAR:
            continue
        state = session.get(SearchState, mag.id)
        nxt = _aware(state.next_search_at) if state else None
        if nxt is None or nxt <= now:
            last = _aware(state.last_search_at) if state else None
            due.append((last or datetime.min.replace(tzinfo=UTC), mag))
    results = []
    for _, mag in sorted(due, key=lambda lm: lm[0])[:max_magazines]:
        try:
            results.append(search_magazine(session, client, mag, today, scheduled=True, now=now))
        except Unavailable as exc:
            log.info("Wanted search stopped: %s", exc)
            break
        except RateLimited:
            break
        except ProwlarrError as exc:
            log.warning("Search for %s failed: %s", mag.title, exc)
    return results
