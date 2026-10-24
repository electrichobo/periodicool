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


def enabled_protocols(session: Session) -> set[str]:
    from periodicool.models import DownloadClient

    return set(
        session.scalars(select(DownloadClient.protocol).where(DownloadClient.enabled.is_(True)))
    )


# Sonarr's three switches per indexer entry: which one each kind of call needs.
TOGGLES = {
    "rss": ("enable_rss", "RSS"),
    "automatic": ("enable_automatic_search", "automatic search"),
    "interactive": ("enable_interactive_search", "interactive search"),
}


def _clients(source, mode: str | None = None) -> list:
    """A Sources object gives every enabled indexer entry with `mode` (rss, automatic,
    interactive) switched on; a single client is itself. Torznab entries are left out
    while no torrent client is enabled."""
    from periodicool.sources import Sources

    if isinstance(source, Sources):
        protocols = source.protocols()
        toggle = TOGGLES[mode][0] if mode else None
        return [
            c
            for c in source.indexers
            if getattr(c, "protocol", "usenet") in protocols | {"usenet"}
            and (getattr(c, "kind", "") != "torznab" or "torrent" in protocols)
            and (toggle is None or getattr(getattr(c, "row", None), toggle, True))
        ]
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
    protocol: str = "usenet"

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
    if getattr(client, "kind", "") in ("newznab", "torznab"):
        return [_newznab_limit(session, client, now)]
    # Torrent indexers only count when a torrent client is enabled, so a usenet-only
    # setup spends no API hits on them.
    protocols = {"usenet"} | ({"torrent"} if "torrent" in enabled_protocols(session) else set())
    infos: list[IndexerInfo] = [
        i
        for i in client.indexers(category)
        if i.enabled and i.protocol in protocols and i.supports_category
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
                protocol=i.protocol,
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
        protocol=getattr(client, "protocol", "usenet"),
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
    rel.seeders, rel.peers = f.seeders, f.peers
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


def _evaluate(
    session,
    title,
    size,
    protocol,
    published,
    magazines,
    today,
    options,
    guid=None,
    seeders=None,
    min_seeders=None,
):
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
        seeders=seeders,
        min_seeders=min_seeders,
    )


def min_seeders(client) -> int | None:
    """The indexer entry's minimum seeders for torrents (None: the default)."""
    row = getattr(client, "row", None)
    value = ((row.settings if row is not None else None) or {}).get("min_seeders")
    return int(value) if isinstance(value, int | float) else None


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
    from periodicool import activity_feed

    for client in _clients(source, "rss"):
        if why := _backoff_reason(client, now):
            result.skipped[_name(client)] = why
            continue
        activity_feed.say(f"{_name(client)}: reading the RSS feed")
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
                found = [replace(f, indexer=lim.name, protocol=lim.protocol) for f in found]
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
                    f.seeders,
                    min_seeders(client),
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


MAX_YEAR_QUERIES = 4  # extra queries per search, one per wanted year, newest first


def search_queries(
    session: Session, mag: Magazine, today: date, issue_keys: list[str] | None = None
) -> list[str]:
    """What to ask the indexers. A title alone only brings back the newest releases, so
    each wanted year gets its own query ("Linux Magazine 2023"); a numbered issue asked
    for by itself also gets "Linux Magazine 311"."""
    terms = list(dict.fromkeys([mag.search_term or default_search_term(mag), *(mag.aliases or [])]))
    if issue_keys:
        issues = list(
            session.scalars(
                select(Issue).where(Issue.magazine_id == mag.id, Issue.key.in_(issue_keys))
            )
        )
    else:
        issues = wanted_issues(session, mag, today)
    years = sorted({i.year for i in issues if i.year}, reverse=True)[:MAX_YEAR_QUERIES]
    numbers = [i.issue_number for i in issues if i.issue_number] if issue_keys else []
    queries = []
    for term in terms:
        queries.append(term)
        queries += [f"{term} {year}" for year in years]
        queries += [f"{term} {number}" for number in numbers[:MAX_YEAR_QUERIES]]
    return list(dict.fromkeys(queries))


def search_magazine(
    session: Session,
    source,
    mag: Magazine,
    today: date,
    *,
    force: bool = False,
    scheduled: bool = False,
    now: datetime | None = None,
    mode: str = "automatic",
    issue_keys: list[str] | None = None,
    queries: list[str] | None = None,
) -> SearchResult:
    """Search one magazine live on every enabled indexer entry with `mode` (automatic or
    interactive search) switched on: the title, plus one query per wanted year (or for
    the asked-for issues). Results are never served from an old search; the indexer
    limits, backoff and daily cap still apply. Raises Unavailable when no entry could
    search. `force` is accepted for older callers and changes nothing."""
    from periodicool import activity_feed

    now = now or _now()
    result = SearchResult(magazine=mag)
    magazines = list(session.scalars(select(Magazine)))
    options = load_options(session)
    cats = _categories(session)
    queries = queries or search_queries(session, mag, today, issue_keys)
    clients = _clients(source, mode)
    if not clients:
        if _clients(source):
            raise Unavailable(f"no indexer has {TOGGLES[mode][1]} on (Settings, Indexers)")
        raise Unavailable("no indexer is set up (Settings, Indexers)")
    problems: list[str] = []
    answered = False
    seen_ids: set[int] = set()

    def add(rel: Release, ev: Evaluation) -> None:
        if rel.id not in seen_ids:
            seen_ids.add(rel.id)
            result.candidates.append((rel, ev))

    for query in queries:
        for client in clients:
            if why := _backoff_reason(client, now):
                result.skipped[_name(client)] = why
                problems.append(why)
                continue
            cap = get_setting(session, DAILY_CAP_KEY)
            if cap is not None and _searches_today(session, now) >= cap:
                raise Unavailable(f"daily search cap of {cap} reached")
            activity_feed.say(f"{_name(client)}: searching \u201c{query}\u201d")
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
                found = client.search(query, cats, [u.id for u in usable])
            except RateLimited:
                _rate_limited(session, now, client)
                problems.append(f"{_name(client)}: rate limited")
                continue
            except ProwlarrError as exc:
                problems.append(f"{_name(client)}: {exc}")
                continue
            _clear_backoff(session, client)
            answered = True
            for i, f in enumerate(found, 1):
                if i == 1 or i % 10 == 0 or i == len(found):
                    activity_feed.say(f"Processing release {i}/{len(found)}")
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
                    f.seeders,
                    min_seeders(client),
                )
                rel, new = _upsert(session, f, ReleaseSource.SEARCH, ev, now, client.source_id)
                session.flush()
                add(rel, ev)
                if new and rel.accepted:
                    result.new_accepted += 1

    if not answered:
        raise Unavailable("; ".join(dict.fromkeys(problems)) or "no indexer could search")
    for problem in problems:
        log.info("Search for %s: %s", mag.title, problem)
    accepted = sum(1 for r, _ in result.candidates if r.accepted)
    activity_feed.say(
        f"Search for {mag.title} done: {len(result.candidates)} releases, {accepted} accepted"
    )
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


def search_term(
    session: Session, source, term: str, today: date, now: datetime | None = None
) -> tuple[list[str], str | None]:
    """Release names an indexer search for `term` finds (for the Add magazine search).
    Goes through search_magazine, so the per-entry cache, limits and backoff apply."""
    probe = Magazine(title=term, aliases=[], search_term=term)
    try:
        result = search_magazine(session, source, probe, today, now=now, mode="interactive")
    except Unavailable as exc:
        return [], str(exc)
    except ProwlarrError as exc:
        return [], str(exc)
    return [rel.title for rel, _ in result.candidates], None
