import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from statistics import median

from depas.commute import from_listing
from depas.config import DEFAULT_COMMON_EXPENSES, db_path
from depas.detail import DETAIL_COLUMNS
from depas.fetch import Fetcher
from depas.models import Listing
from depas.preferences import Preferences, clear_preference, seed_from_env, set_preference
from depas.traits import EXCLUDE

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


# Only what a search card carries; the detail-page columns are owned by save_detail.
FIELDS = (
    "url", "title", "price", "currency", "is_project", "price_clp",
    "bedrooms", "bathrooms", "area_m2", "commune", "address", "image_url",
)


def connect(path: Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or db_path())
    connection.row_factory = sqlite3.Row
    # WAL + a busy timeout because the bot and the cron sidecar share one file.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    migrate(connection)
    # Filled from the environment exactly once, so a box upgrading into this keeps its .env.
    seed_from_env(connection)
    # The view is derived, not state: rebuilt every connect so it tracks the code.
    connection.executescript(RANKED_VIEW)
    sync_lease_income(connection, Preferences.load(connection))
    return connection


def migrate(connection: sqlite3.Connection) -> list[int]:
    """Apply any migrations/*.sql not yet recorded, in filename order."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    newly_applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_")[0])
        if version in applied:
            continue
        connection.executescript(path.read_text())
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (version,),
        )
        connection.commit()
        newly_applied.append(version)
    return newly_applied


RANKED_VIEW = f"""
DROP VIEW IF EXISTS listings_ranked;
CREATE VIEW listings_ranked AS
SELECT *,
       -- Stable: nothing deletes rows or VACUUMs, so a listing keeps its number for life.
       rowid                                    AS id,
       COALESCE(area_useful_m2, area_m2)        AS area,
       -- Antigüedad is published either as a number of years or as the year the
       -- building went up, and no portal says which it means. A flat over a century
       -- old is far rarer than that second habit, so a big number reads as a year;
       -- a year still to come is a typo, hence the floor at zero rather than a
       -- negative age.
       CASE WHEN age_years > 100
            THEN MAX(CAST(strftime('%Y', 'now') AS INTEGER) - age_years, 0)
            ELSE age_years
       END                                      AS age,
       -- Only Portal Inmobiliario publishes a UF/m2 figure; for everyone else derive it
       -- from the cached UF, which matches the published one to well under a percent.
       COALESCE(price_per_m2_uf,
                price_clp / (SELECT value FROM uf_daily ORDER BY day DESC LIMIT 1)
                          / NULLIF(COALESCE(area_useful_m2, area_m2), 0))
                                                AS price_per_m2_uf_effective,
       COALESCE(zone_price_per_m2_uf,
                (SELECT uf_per_m2 FROM zone_benchmark WHERE commune = listings.commune))
                                                AS zone_price_per_m2_uf_effective,
       -- A gasto comun that is absent, or published as zero, is assumed rather than
       -- taken as free: see DEFAULT_COMMON_EXPENSES. The cards say when it is assumed.
       price_clp + COALESCE(NULLIF(common_expenses, 0), {DEFAULT_COMMON_EXPENSES})
                                                AS total_monthly_clp,
       price_clp + COALESCE(NULLIF(common_expenses, 0), {DEFAULT_COMMON_EXPENSES})
           - COALESCE(parking_spaces, 0) * (SELECT value FROM settings WHERE key = 'parking_income')
           - COALESCE(storage_units, 0)  * (SELECT value FROM settings WHERE key = 'storage_income')
                                                AS net_monthly_clp
FROM listings;
"""

# A /dislike is out for good: never announced again, and out of the pool. Not a preference.
NOT_REJECTED = "COALESCE(interest, 0) >= 0"

# Enriched, an actual unit, still published, and not turned down: an unenriched one
# would beat everything, and one already rented is not a candidate however well it grades.
KEPT = ("detail_fetched_at IS NOT NULL AND is_project = 0 AND delisted_at IS NULL "
        f"AND {NOT_REJECTED}")


def pool_query(prefs: Preferences) -> str:
    """Every listing worth ranking or alerting on, minus the traits you rule out."""
    excluded = [f"({trait.keeps})" for trait in prefs.traits(EXCLUDE)]
    return f"SELECT * FROM listings_ranked WHERE {' AND '.join([KEPT, *excluded])}"


# A detail page is the crawl's most expensive request, so it is only ever spent on a
# listing the pool could accept: `KEPT` wants an actual unit nobody turned down that is
# still published, and none of those three can change however long a row waits.
ELIGIBLE_FOR_DETAIL = ("delisted_at IS NULL AND is_project = 0 "
                       "AND COALESCE(interest, 0) >= 0")

QUEUED = ("SELECT portal, external_id, url, detail_fetched_at, price, price_at_detail "
          f"FROM listings WHERE {ELIGIBLE_FOR_DETAIL}")


def pending_detail(connection: sqlite3.Connection, fresh: int,
                   refresh: int = 0) -> list[sqlite3.Row]:
    """The detail pages due: the ones never read first, then the re-reads.

    Two budgets rather than one, because they compete for the same requests and a
    listing nobody has read yet must never wait behind a re-read. A month where a
    thousand rows come due at once would otherwise starve the finds, which are the
    only reason the pass exists.
    """
    unread = connection.execute(
        f"{QUEUED} AND detail_fetched_at IS NULL ORDER BY first_seen DESC LIMIT ?",
        (fresh,),
    ).fetchall()
    if refresh <= 0:
        return unread
    # A price that moved is read before one merely due: until it is, the listing is
    # ranked on a UF/m2 computed from the old price while everything else uses today's.
    due = connection.execute(
        f"{QUEUED} AND detail_fetched_at IS NOT NULL AND detail_due_at <= ? "
        "ORDER BY (price_at_detail IS NOT NULL AND price <> price_at_detail) DESC, "
        "detail_due_at LIMIT ?",
        (datetime.now(UTC).isoformat(), refresh),
    ).fetchall()
    return [*unread, *due]


# ── what a sweep saw, and what that lets us conclude ────────────────────────────


def remember_sweep(connection: sqlite3.Connection, portal: str, commune: str | None,
                   started_at: str, cards_seen: int, error: str | None) -> None:
    """Record one (portal, comuna) sweep: what it saw, and whether it can be believed."""
    connection.execute(
        "INSERT INTO scrape_runs "
        "(portal, commune, started_at, finished_at, cards_seen, ok, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (portal, commune, started_at, datetime.now(UTC).isoformat(), cards_seen,
         int(error is None), error),
    )
    connection.commit()


def mark_delisted(connection: sqlite3.Connection, portal: str, external_id: str) -> None:
    """Take one listing out of the pool: rented, withdrawn, or answering 404."""
    connection.execute(
        "UPDATE listings SET delisted_at = ? "
        "WHERE portal = ? AND external_id = ? AND delisted_at IS NULL",
        (datetime.now(UTC).isoformat(), portal, external_id),
    )
    connection.commit()


def sweep_delisted(connection: sqlite3.Connection, after: int) -> int:
    """Delist what `after` believable sweeps of its portal have failed to turn up.

    A sweep only counts as evidence if it finished and actually saw cards. A portal
    whose markup moved returns zero of them and raises nothing, which is
    indistinguishable from a comuna with no listings — so neither is allowed to
    delist anything. That leaves stale rows around longer than necessary, which is
    the direction to err in: the cost of a false positive is dropping a flat
    somebody starred.
    """
    # Not merely pointless but dangerous: `COUNT(*) >= 0` is true of every row, so
    # asking for zero sweeps of evidence would delist the whole database.
    if after <= 0:
        return 0
    delisted = connection.execute(
        "UPDATE listings SET delisted_at = ? WHERE delisted_at IS NULL AND ("
        "  SELECT COUNT(*) FROM scrape_runs"
        "   WHERE scrape_runs.portal = listings.portal"
        "     AND ok = 1 AND cards_seen > 0"
        "     AND started_at > listings.last_seen) >= ?",
        (datetime.now(UTC).isoformat(), after),
    ).rowcount
    connection.commit()
    return delisted


def quiet_portals(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Portals whose last sweep saw nothing where an earlier one saw plenty.

    `save` cannot tell a portal that changed its markup from a comuna that emptied
    out: both arrive as no listings at all, and the pass completes either way. This
    is the comparison that separates them.
    """
    return connection.execute(
        "SELECT portal, MAX(started_at) AS last_swept,"
        "       SUM(cards_seen) AS cards_this_time,"
        "       (SELECT MAX(cards_seen) FROM scrape_runs AS before"
        "         WHERE before.portal = scrape_runs.portal) AS cards_at_best"
        "  FROM scrape_runs"
        " WHERE started_at = (SELECT MAX(started_at) FROM scrape_runs AS latest"
        "                      WHERE latest.portal = scrape_runs.portal)"
        " GROUP BY portal"
        " HAVING cards_this_time = 0 AND cards_at_best > 0"
    ).fetchall()


# ── whether a conditional GET would ever pay here ───────────────────────────────
# Validators only, never a response body: an ETag is ~30 bytes for a page that costs
# hundreds of kilobytes, so 20 000 listings come to about 4 MB.
#
# Sending them back is not wired up yet, and the reason is the portals rather than the
# storage. `fetch_detail` both fetches and parses, and assetplan and toctoc read two
# urls per listing — a 304 on one of them would leave the parser with no body and no way
# to rebuild the rest. Doing it properly means splitting fetching from parsing in the
# portal interface, which is its own change. This records what the portals offer so that
# change can be justified, or dropped, on evidence.


def remember_validators(connection: sqlite3.Connection,
                        seen: Mapping[str, tuple[str | None, str | None, int]]) -> int:
    """Record the cache validators each url offered on this pass."""
    now = datetime.now(UTC).isoformat()
    connection.executemany(
        "INSERT INTO http_cache (url, etag, last_modified, status, fetched_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
        "etag = excluded.etag, last_modified = excluded.last_modified, "
        "status = excluded.status, fetched_at = excluded.fetched_at",
        [(url, etag, last_modified, status, now)
         for url, (etag, last_modified, status) in seen.items()],
    )
    connection.commit()
    return len(seen)


def validator_coverage(connection: sqlite3.Connection) -> tuple[int, int]:
    """How many urls we have seen, and how many of them offered a validator at all."""
    row = connection.execute(
        "SELECT COUNT(*) AS urls, "
        "SUM(CASE WHEN etag IS NOT NULL OR last_modified IS NOT NULL THEN 1 ELSE 0 END)"
        " AS offered FROM http_cache"
    ).fetchone()
    return row["urls"], row["offered"] or 0


def refresh_zone_benchmarks(connection: sqlite3.Connection) -> int:
    """Recompute each commune's median published zone UF/m2 for the other portals to borrow."""
    by_commune: dict[str, list[float]] = defaultdict(list)
    for commune, value in connection.execute(
        "SELECT commune, zone_price_per_m2_uf FROM listings "
        "WHERE commune IS NOT NULL AND zone_price_per_m2_uf IS NOT NULL"
    ):
        by_commune[commune].append(value)
    connection.executemany(
        "INSERT INTO zone_benchmark (commune, uf_per_m2) VALUES (?, ?) "
        "ON CONFLICT(commune) DO UPDATE SET uf_per_m2 = excluded.uf_per_m2",
        [(commune, median(values)) for commune, values in by_commune.items()],
    )
    connection.commit()
    return len(by_commune)


def refresh_commutes(connection: sqlite3.Connection, fetcher: Fetcher,
                     prefs: Preferences, limit: int) -> int:
    """Route the located listings still missing travel times, newest first, up to `limit`."""
    places = prefs.locations()
    wanted = {place.name for place in places}
    if not wanted:
        return 0
    rows = connection.execute(
        "SELECT rowid, lat, lon, commute FROM listings "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL ORDER BY first_seen DESC"
    ).fetchall()
    stale = [row for row in rows
             if not row["commute"] or set(json.loads(row["commute"])) != wanted][:limit]
    for row in stale:
        connection.execute(
            "UPDATE listings SET commute = ? WHERE rowid = ?",
            (json.dumps(from_listing(fetcher, row["lat"], row["lon"], places)),
             row["rowid"]),
        )
    connection.commit()
    return len(stale)


def sync_lease_income(connection: sqlite3.Connection, prefs: Preferences) -> None:
    """Mirror the sublet income into `settings` so the ranked view can read it from SQL."""
    for kind in ("parking", "storage"):
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (prefs.lease_income(kind), f"{kind}_income"),
        )
    connection.commit()


def store_preference(connection: sqlite3.Connection, name: str, raw: str) -> object | None:
    """Write one setting and push whatever the ranked view reads from SQL back into it."""
    value = set_preference(connection, name, raw)
    sync_lease_income(connection, Preferences.load(connection))
    return value


def forget_preference(connection: sqlite3.Connection, name: str) -> object | None:
    """Clear one setting back to its default, re-mirroring for the same reason."""
    clear_preference(connection, name)
    prefs = Preferences.load(connection)
    sync_lease_income(connection, prefs)
    return prefs.value(name)


def fill_gaps(connection: sqlite3.Connection, portal: str, external_id: str,
              gaps: Mapping[str, object]) -> None:
    """Write columns read out of a description the row already carries.

    Deliberately not `save_detail`: nothing was fetched. Routing this through the
    detail writer would compute the digest from these few columns rather than from a
    whole page — making the next real reading look like everything changed — and would
    push `detail_due_at` a backoff into the future for work that touched no portal.
    The changes are not recorded either: an inferred value is ours, not the portal's.
    """
    columns = [name for name in gaps if name in DETAIL_COLUMNS or name in ("lat", "lon")]
    if not columns:
        return
    connection.execute(
        f"UPDATE listings SET {', '.join(f'{name} = ?' for name in columns)} "
        "WHERE portal = ? AND external_id = ?",
        [*(gaps[name] for name in columns), portal, external_id],
    )
    connection.commit()


def detail_digest(detail: Mapping[str, object]) -> str:
    """A digest of what a detail page said, not of the page.

    Hashing the HTML would answer "did anything change" with yes every single time: a
    portal page carries CSRF tokens, view counters and render timestamps. The parsed
    fields are what we actually care about having changed, and they survive a redesign
    that moves no data.
    """
    payload = json.dumps({name: value for name, value in sorted(detail.items())
                          if name != "detail_fetched_at"},
                         sort_keys=True, default=str, ensure_ascii=False)
    return sha256(payload.encode()).hexdigest()


# How long a listing that keeps coming back unchanged is left alone, doubling each time
# it does. A flat idle for two months is worth a look monthly; one that moved yesterday
# is worth one in three days, and the same budget then covers far more of them.
REFRESH_DAYS, MAX_BACKOFF_DOUBLINGS = 3, 3


def next_detail_read(unchanged_in_a_row: int) -> str:
    """When to read this detail page again, given how long it has been standing still."""
    days = REFRESH_DAYS * 2 ** min(unchanged_in_a_row, MAX_BACKOFF_DOUBLINGS)
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _record_changes(connection: sqlite3.Connection, portal: str, external_id: str,
                    before: sqlite3.Row | None, detail: Mapping[str, object],
                    columns: list[str]) -> int:
    """Append one row per field that actually moved; the current value stays on `listings`."""
    # A first reading is not a change: every column goes from NULL to whatever the portal
    # published, and logging forty of those per listing would bury the real ones.
    if before is None or before["detail_fetched_at"] is None:
        return 0
    now = datetime.now(UTC).isoformat()
    # A sqlite3.Row tests `in` against its values, not its column names, so the names
    # are taken once and asked as a set.
    stored = set(before.keys())
    moved = [(name, before[name], detail[name]) for name in columns
             if name in stored and before[name] != detail[name]]
    connection.executemany(
        "INSERT INTO detail_changes "
        "(portal, external_id, field, old_value, new_value, changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(portal, external_id, name,
          None if was is None else str(was), None if now_value is None else str(now_value),
          now)
         for name, was, now_value in moved],
    )
    return len(moved)


def save_detail(
    connection: sqlite3.Connection, portal: str, external_id: str, detail: dict[str, object],
    *, unchanged: bool = False,
) -> int:
    """Write one listing's detail-page fields onto its row, recording what moved.

    `unchanged` is for a page the portal answered 304 to, or whose digest matched: there
    is nothing to write but the row still earns a longer wait before the next read.
    """
    before = connection.execute(
        "SELECT * FROM listings WHERE portal = ? AND external_id = ?", (portal, external_id)
    ).fetchone()
    if before is None:
        return 0
    columns = [name for name in detail if name in DETAIL_COLUMNS or name in ("lat", "lon")]
    changed = 0 if unchanged else _record_changes(
        connection, portal, external_id, before, detail, columns)
    # A digest that matched, or a 304, is a listing standing still; anything that moved
    # resets the count so the next read comes round soon.
    standing_still = unchanged or (before["detail_fetched_at"] is not None and changed == 0)
    unchanged_in_a_row = (before["detail_unchanged_count"] + 1) if standing_still else 0

    assignments = [f"{name} = ?" for name in columns] if not unchanged else []
    connection.execute(
        f"UPDATE listings SET {''.join(f'{one}, ' for one in assignments)}"
        "detail_fetched_at = ?, price_at_detail = price, detail_hash = ?, "
        "detail_unchanged_count = ?, detail_due_at = ? "
        "WHERE portal = ? AND external_id = ?",
        [*([] if unchanged else [detail[name] for name in columns]),
         datetime.now(UTC).isoformat(),
         before["detail_hash"] if unchanged else detail_digest(detail),
         unchanged_in_a_row, next_detail_read(unchanged_in_a_row), portal, external_id],
    )
    connection.commit()
    return changed


# SQLite caps how many values one statement may bind. No single portal answers with
# this many, but a sweep of several comunas can, so the lookup goes in chunks.
LOOKUP_CHUNK = 500


def _stored_prices(connection: sqlite3.Connection,
                   listings: Iterable[Listing]) -> dict[tuple[str, str], float]:
    """What we last stored for each of these, read in one query per portal per chunk."""
    by_portal: dict[str, list[str]] = defaultdict(list)
    for listing in listings:
        by_portal[listing.portal].append(listing.external_id)

    stored: dict[tuple[str, str], float] = {}
    for portal, external_ids in by_portal.items():
        for start in range(0, len(external_ids), LOOKUP_CHUNK):
            chunk = external_ids[start:start + LOOKUP_CHUNK]
            stored.update(
                ((portal, row["external_id"]), row["price"])
                for row in connection.execute(
                    "SELECT external_id, price FROM listings WHERE portal = ? "
                    f"AND external_id IN ({', '.join('?' * len(chunk))})",
                    (portal, *chunk),
                )
            )
    return stored


def save(connection: sqlite3.Connection, listings: Iterable[Listing]) -> dict[str, int]:
    """Upsert listings, recording a price_history row whenever the price moves."""
    now = datetime.now(UTC).isoformat()
    counts = {"new": 0, "price_changed": 0, "unchanged": 0}

    # A listing shows up twice when it matches two of the comunas swept, and only Portal
    # Inmobiliario dedupes its own pages. Last one wins, as it did when each was upserted
    # in turn — but now it costs one write instead of two.
    unique = {(listing.portal, listing.external_id): listing for listing in listings}
    # One query for the whole batch rather than one per listing: cheap against a local
    # SQLite, a round-trip each against anything over a socket.
    stored = _stored_prices(connection, unique.values())

    for key, listing in unique.items():
        previous = stored.get(key)
        values = [getattr(listing, name) for name in FIELDS]
        if key not in stored:
            connection.execute(
                f"INSERT INTO listings (portal, external_id, {', '.join(FIELDS)}, "
                "first_seen, last_seen) "
                f"VALUES (?, ?, {', '.join('?' * len(FIELDS))}, ?, ?)",
                [*key, *values, now, now],
            )
            counts["new"] += 1
        else:
            # delisted_at is cleared unconditionally: a sweep seeing the listing is the
            # last word on whether it is still published, which is what makes a portal
            # outage or a comuna dropped and restored heal itself rather than need a fix.
            connection.execute(
                f"UPDATE listings SET {', '.join(f'{name} = ?' for name in FIELDS)}, "
                "last_seen = ?, delisted_at = NULL "
                "WHERE portal = ? AND external_id = ?",
                [*values, now, *key],
            )
            moved = previous != listing.price
            counts["price_changed" if moved else "unchanged"] += 1
            if moved:
                # The detail page's UF/m2 was computed from the old price, so the row is
                # now ranked on two prices at once. Read it again on the next pass.
                connection.execute(
                    "UPDATE listings SET detail_due_at = '' "
                    "WHERE portal = ? AND external_id = ? AND detail_fetched_at IS NOT NULL",
                    key,
                )

        if key not in stored or previous != listing.price:
            connection.execute(
                "INSERT INTO price_history "
                "(portal, external_id, price, currency, price_clp, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [*key, listing.price, listing.currency, listing.price_clp, now],
            )

    connection.commit()
    return counts


def mark_notified(connection: sqlite3.Connection, portal: str, external_id: str) -> None:
    connection.execute(
        "UPDATE listings SET notified_at = ? WHERE portal = ? AND external_id = ?",
        (datetime.now(UTC).isoformat(), portal, external_id),
    )
    connection.commit()


# What the chat commands mean, as stored in `listings.interest`.
LIKE, DISLIKE = 1, -1


def set_interest(connection: sqlite3.Connection, portal: str, external_id: str,
                 interest: int | None, rated_by: str | None = None) -> None:
    """Record the verdict somebody gave a listing from the chat, or None to undo it."""
    connection.execute(
        "UPDATE listings SET interest = ?, rated_at = ?, rated_by = ? "
        "WHERE portal = ? AND external_id = ?",
        (interest, datetime.now(UTC).isoformat(), rated_by, portal, external_id),
    )
    connection.commit()


def remember_card(connection: sqlite3.Connection, chat_id: object, message_id: int,
                  portal: str, external_id: str, is_photo: bool = False) -> None:
    """Record a card we posted, so a command left under it can find its listing."""
    connection.execute(
        "INSERT INTO card_messages "
        "(chat_id, message_id, portal, external_id, is_photo, posted_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, message_id) DO NOTHING",
        (str(chat_id), message_id, portal, external_id, int(is_photo),
         datetime.now(UTC).isoformat()),
    )
    connection.commit()


def remember_breakdown(connection: sqlite3.Connection, chat_id: object, message_id: int,
                       detail_chat_id: object, detail_message_id: int) -> None:
    """Record the breakdown posted under a card, so a redraw can re-render it in place."""
    connection.execute(
        "UPDATE card_messages SET detail_chat_id = ?, detail_message_id = ? "
        "WHERE chat_id = ? AND message_id = ?",
        (str(detail_chat_id), detail_message_id, str(chat_id), message_id),
    )
    connection.commit()


def link_thread(connection: sqlite3.Connection, chat_id: object, message_id: int,
                thread_chat_id: object, thread_id: int) -> bool:
    """Pair a channel card with the discussion-group copy its comments hang off."""
    updated = connection.execute(
        "UPDATE card_messages SET thread_chat_id = ?, thread_id = ? "
        "WHERE chat_id = ? AND message_id = ?",
        (str(thread_chat_id), thread_id, str(chat_id), message_id),
    ).rowcount
    connection.commit()
    return bool(updated)


def card_for_thread(connection: sqlite3.Connection, chat_id: object,
                    thread_id: int) -> sqlite3.Row | None:
    """The card a Comments thread belongs to."""
    return connection.execute(
        "SELECT * FROM card_messages WHERE thread_chat_id = ? AND thread_id = ?",
        (str(chat_id), thread_id),
    ).fetchone()


def card_for_message(connection: sqlite3.Connection, chat_id: object,
                     message_id: int) -> sqlite3.Row | None:
    """The card a message replies to, when we are the one who posted it."""
    return connection.execute(
        "SELECT * FROM card_messages WHERE chat_id = ? AND message_id = ?",
        (str(chat_id), message_id),
    ).fetchone()


# The pinned ⭐ list, kept in `settings` beside the poll offset: two integers, no table.
SHORTLIST_CHAT, SHORTLIST_MESSAGE = "shortlist_chat_id", "shortlist_message_id"


def remember_shortlist(connection: sqlite3.Connection, chat_id: object,
                       message_id: int) -> None:
    """Record the message the ⭐ list lives in, so the next verdict edits it rather than posts."""
    connection.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ((SHORTLIST_CHAT, int(chat_id)), (SHORTLIST_MESSAGE, message_id)),
    )
    connection.commit()


def stored_shortlist(connection: sqlite3.Connection) -> tuple[str, int] | None:
    """Where the pinned ⭐ list is, or None until one has been posted."""
    found = dict(connection.execute(
        "SELECT key, value FROM settings WHERE key IN (?, ?)",
        (SHORTLIST_CHAT, SHORTLIST_MESSAGE),
    ).fetchall())
    if SHORTLIST_CHAT not in found or SHORTLIST_MESSAGE not in found:
        return None
    return str(found[SHORTLIST_CHAT]), found[SHORTLIST_MESSAGE]


def clear_notified(connection: sqlite3.Connection, hours: int) -> int:
    """Un-stamp recently announced listings so the next watch pass posts them again."""
    # Same isoformat the stamp was written with, so the comparison stays lexicographic.
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    cleared = connection.execute(
        "UPDATE listings SET notified_at = NULL WHERE notified_at >= ?", (cutoff,)
    ).rowcount
    connection.commit()
    return cleared


# The watch's own heartbeat, in `settings` beside the shortlist: what `healthcheck` reads.
# A pass that dies mid-way still updates `last_seen`, so freshness there proves nothing.
WATCH_COMPLETED, WATCH_ERROR = "watch_completed_at", "watch_error"

# The stages a pass is made of, each able to run on its own schedule. `watch` is all of
# them in order and keeps the original two keys, so a box upgrading into this does not
# read as having never completed a pass.
WATCH = "watch"
STAGES = (WATCH, "discover", "enrich", "route", "announce")

# How long each may go without completing before the admins hear about it. Discovery is
# the one that must not stall — everything downstream is fed by it — while routing is
# somebody else's server and allowed to be slow.
STALE_HOURS = {WATCH: 4, "discover": 4, "enrich": 6, "route": 24, "announce": 6}


def _keys(stage: str) -> tuple[str, str]:
    if stage == WATCH:
        return WATCH_COMPLETED, WATCH_ERROR
    return f"{WATCH_COMPLETED}:{stage}", f"{WATCH_ERROR}:{stage}"


def remember_watch(connection: sqlite3.Connection, error: str | None,
                   stage: str = WATCH) -> None:
    """Record how a stage ended: the time it finished, or what stopped it."""
    completed, failed = _keys(stage)
    key, value = ((failed, error) if error
                  else (completed, datetime.now(UTC).isoformat()))
    connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    connection.commit()


def stored_watch(connection: sqlite3.Connection,
                 stage: str = WATCH) -> tuple[str | None, str | None]:
    """When this stage last completed, and what stopped the last one that did not."""
    completed, failed = _keys(stage)
    found = dict(connection.execute(
        "SELECT key, value FROM settings WHERE key IN (?, ?)", (completed, failed)
    ).fetchall())
    return found.get(completed), found.get(failed)


def stale_stages(connection: sqlite3.Connection, hours: int | None = None
                 ) -> list[tuple[str, str | None, str | None]]:
    """Every stage that has gone too long without completing, and what stopped it.

    A *sub*-stage nobody runs is not stale: splitting the pass up is opt-in, so a box
    still on one hourly `watch` must not be warned about four stages that never existed.
    `watch` itself is always checked, unstamped included — a deploy whose pass has never
    finished is the case this watchdog was built for.
    Same isoformat the stamps were written with, so the comparison stays lexicographic.
    """
    stale = []
    for stage in STAGES:
        completed, error = stored_watch(connection, stage)
        if stage != WATCH and completed is None and error is None:
            continue
        cutoff = (datetime.now(UTC)
                  - timedelta(hours=hours if hours is not None else STALE_HOURS[stage]))
        if completed is None or completed < cutoff.isoformat():
            stale.append((stage, completed, error))
    return stale
