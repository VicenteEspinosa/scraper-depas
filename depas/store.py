import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from depas.commute import from_listing
from depas.config import DEFAULT_COMMON_EXPENSES, db_path
from depas.fetch import Fetcher
from depas.detail import DETAIL_COLUMNS
from depas.models import Listing
from depas.preferences import Preferences, clear_preference, seed_from_env, set_preference

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


# Only what a search card actually carries. Detail-page columns (gastos comunes,
# coordinates, specs) are owned by save_detail — listing them here would blank
# them on the next re-scrape, because the card has nothing to put in their place.
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
    # The configuration lives in the database now, so a box upgrading into this keeps
    # whatever its .env said: the table is filled from the environment exactly once.
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

# Amoblado is a hard no rather than a preference: a furnished flat is excluded
# outright, and it is kept out of the pool everything else is ranked against too,
# so percentiles are measured against apartments we would actually take. An
# undeclared `furnished` is not a reason to drop a listing, but a title that says
# amoblado is — the portals that publish no spec row for it still say it there.
NOT_FURNISHED = ("COALESCE(furnished, 0) = 0"
                 " AND (title IS NULL OR lower(title) NOT LIKE '%amoblad%')")

# A listing turned down with /dislike is out for good: never announced again, and
# kept out of the pool the others are ranked against, so a flat nobody would take
# stops moving the percentiles the rest are graded on.
NOT_REJECTED = "COALESCE(interest, 0) >= 0"

# Every listing worth ranking or alerting on: enriched, an actual unit rather than
# a project, not furnished and not rejected. An unenriched listing would be graded
# on two components and beat everything.
POOL_QUERY = ("SELECT * FROM listings_ranked "
              f"WHERE detail_fetched_at IS NOT NULL AND is_project = 0 "
              f"AND {NOT_FURNISHED} AND {NOT_REJECTED}")


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
    """Route the located listings still missing travel times, newest first, up to `limit`.

    Coordinates never move, so an answer is kept for the life of the listing; only a
    change to the configured locations makes a stored one stale. Routing is a call to
    somebody else's server per listing per location, which is why this is capped rather
    than a full recompute.
    """
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
    """Mirror the sublet income into `settings` so the ranked view can read it from SQL.

    `net_monthly_clp` is a column of a view, so the figures it subtracts have to be
    reachable from SQL. Called on connect and again whenever either one is edited,
    because a long-running bot would otherwise keep grading on the startup value.
    """
    for kind in ("parking", "storage"):
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (prefs.lease_income(kind), f"{kind}_income"),
        )
    connection.commit()


def store_preference(connection: sqlite3.Connection, name: str, raw: str) -> object | None:
    """Write one setting and push whatever the ranked view reads from SQL back into it.

    The one write path every surface should use -- the CLI today, the chat commands
    next -- because `net_monthly_clp` is a view column: editing the sublet income and
    not re-mirroring it leaves the grading running on the value from startup.
    """
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
        f"UPDATE listings SET {', '.join(f'{name} = ?' for name in columns)}, detail_fetched_at = ? "
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
                f"INSERT INTO listings (portal, external_id, {', '.join(FIELDS)}, first_seen, last_seen) "
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
                 interest: int, rated_by: str | None = None) -> None:
    """Record the verdict somebody gave a listing from the chat."""
    connection.execute(
        "UPDATE listings SET interest = ?, rated_at = ?, rated_by = ? "
        "WHERE portal = ? AND external_id = ?",
        (interest, datetime.now(UTC).isoformat(), rated_by, portal, external_id),
    )
    connection.commit()


def remember_card(connection: sqlite3.Connection, chat_id: object, message_id: int,
                  portal: str, external_id: str, is_photo: bool = False) -> None:
    """Record a card we posted, so a command left under it can find its listing.

    Ids come from Telegram's own record of the message rather than from whatever
    TELEGRAM_CHAT_ID holds, which may be an @username the forwards never mention.
    """
    connection.execute(
        "INSERT INTO card_messages "
        "(chat_id, message_id, portal, external_id, is_photo, posted_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, message_id) DO NOTHING",
        (str(chat_id), message_id, portal, external_id, int(is_photo),
         datetime.now(UTC).isoformat()),
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


def clear_notified(connection: sqlite3.Connection, hours: int) -> int:
    """Un-stamp recently announced listings so the next watch pass posts them again."""
    # Same isoformat the stamp was written with, so the comparison stays lexicographic.
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    cleared = connection.execute(
        "UPDATE listings SET notified_at = NULL WHERE notified_at >= ?", (cutoff,)
    ).rowcount
    connection.commit()
    return cleared
