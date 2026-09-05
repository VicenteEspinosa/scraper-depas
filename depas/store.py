import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
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
                                                AS net_monthly_clp,
       -- price_history gains a row every time the asking price moves, so the last one
       -- reading differently is the figure this listing changed *from*. Same currency
       -- only: a UF listing re-published in pesos is a different figure, not a discount.
       (SELECT h.price FROM price_history h
         WHERE h.portal = listings.portal AND h.external_id = listings.external_id
           AND h.currency = listings.currency AND h.price <> listings.price
         ORDER BY h.seen_at DESC LIMIT 1)       AS previous_price,
       -- When the price it carries now was first seen, which is when it moved.
       (SELECT MAX(h.seen_at) FROM price_history h
         WHERE h.portal = listings.portal AND h.external_id = listings.external_id
           AND h.price = listings.price)        AS price_changed_at
FROM listings;
"""

# A /dislike is out for good: never announced again, and out of the pool. Not a preference.
NOT_REJECTED = "COALESCE(interest, 0) >= 0"

# Enriched, an actual unit, and not turned down: an unenriched one would beat everything.
KEPT = ("detail_fetched_at IS NOT NULL AND is_project = 0 "
        f"AND {NOT_REJECTED}")


def pool_query(prefs: Preferences) -> str:
    """Every listing worth ranking or alerting on, minus the traits you rule out."""
    excluded = [f"({trait.keeps})" for trait in prefs.traits(EXCLUDE)]
    return f"SELECT * FROM listings_ranked WHERE {' AND '.join([KEPT, *excluded])}"


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


def save_detail(
    connection: sqlite3.Connection, portal: str, external_id: str, detail: dict[str, object]
) -> None:
    """Write one listing's detail-page fields onto its existing row."""
    columns = [name for name in detail if name in DETAIL_COLUMNS or name in ("lat", "lon")]
    connection.execute(
        f"UPDATE listings SET {', '.join(f'{name} = ?' for name in columns)}, "
        "detail_fetched_at = ? "
        "WHERE portal = ? AND external_id = ?",
        [*(detail[name] for name in columns), datetime.now(UTC).isoformat(), portal, external_id],
    )
    connection.commit()


def save(connection: sqlite3.Connection, listings: Iterable[Listing]) -> dict[str, int]:
    """Upsert listings, recording a price_history row whenever the price moves."""
    now = datetime.now(UTC).isoformat()
    counts = {"new": 0, "price_changed": 0, "unchanged": 0}

    for listing in listings:
        key = (listing.portal, listing.external_id)
        previous = connection.execute(
            "SELECT price FROM listings WHERE portal = ? AND external_id = ?", key
        ).fetchone()

        values = [getattr(listing, name) for name in FIELDS]
        if previous is None:
            connection.execute(
                f"INSERT INTO listings (portal, external_id, {', '.join(FIELDS)}, "
                "first_seen, last_seen) "
                f"VALUES (?, ?, {', '.join('?' * len(FIELDS))}, ?, ?)",
                [*key, *values, now, now],
            )
            counts["new"] += 1
        else:
            connection.execute(
                f"UPDATE listings SET {', '.join(f'{name} = ?' for name in FIELDS)}, last_seen = ? "
                "WHERE portal = ? AND external_id = ?",
                [*values, now, *key],
            )
            counts["price_changed" if previous["price"] != listing.price else "unchanged"] += 1

        if previous is None or previous["price"] != listing.price:
            connection.execute(
                "INSERT INTO price_history (portal, external_id, price, currency, seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [*key, listing.price, listing.currency, now],
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
