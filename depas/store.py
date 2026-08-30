import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from depas.commute import from_listing
from depas.config import lease_income, locations
from depas.fetch import Fetcher
from depas.detail import DETAIL_COLUMNS
from depas.models import Listing

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def db_path() -> Path:
    return Path(os.environ.get("DEPAS_DB_PATH", "depas.db"))


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
    # The view is derived, not state: rebuilt every connect so it tracks the code.
    connection.executescript(RANKED_VIEW)
    _sync_lease_income(connection)
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


RANKED_VIEW = """
DROP VIEW IF EXISTS listings_ranked;
CREATE VIEW listings_ranked AS
SELECT *,
       -- Stable: nothing deletes rows or VACUUMs, so a listing keeps its number for life.
       rowid                                    AS id,
       COALESCE(area_useful_m2, area_m2)        AS area,
       -- Only Portal Inmobiliario publishes a UF/m2 figure; for everyone else derive it
       -- from the cached UF, which matches the published one to well under a percent.
       COALESCE(price_per_m2_uf,
                price_clp / (SELECT value FROM uf_daily ORDER BY day DESC LIMIT 1)
                          / NULLIF(COALESCE(area_useful_m2, area_m2), 0))
                                                AS price_per_m2_uf_effective,
       COALESCE(zone_price_per_m2_uf,
                (SELECT uf_per_m2 FROM zone_benchmark WHERE commune = listings.commune))
                                                AS zone_price_per_m2_uf_effective,
       price_clp + COALESCE(common_expenses, 0) AS total_monthly_clp,
       price_clp + COALESCE(common_expenses, 0)
           - COALESCE(parking_spaces, 0) * (SELECT value FROM settings WHERE key = 'parking_income')
           - COALESCE(storage_units, 0)  * (SELECT value FROM settings WHERE key = 'storage_income')
                                                AS net_monthly_clp
FROM listings;
"""


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


def refresh_commutes(connection: sqlite3.Connection, fetcher: Fetcher, limit: int) -> int:
    """Route the located listings still missing travel times, newest first, up to `limit`.

    Coordinates never move, so an answer is kept for the life of the listing; only a
    change to DEPAS_LOCATIONS makes a stored one stale. Routing is a call to somebody
    else's server per listing per location, which is why this is capped rather than a
    full recompute.
    """
    wanted = {place.name for place in locations()}
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
            (json.dumps(from_listing(fetcher, row["lat"], row["lon"])), row["rowid"]),
        )
    connection.commit()
    return len(stale)


def _sync_lease_income(connection: sqlite3.Connection) -> None:
    """Mirror the environment into `settings` so the ranked view can read it from SQL."""
    for kind in ("parking", "storage"):
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?", (lease_income(kind), f"{kind}_income")
        )
    connection.commit()




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


def clear_notified(connection: sqlite3.Connection, hours: int) -> int:
    """Un-stamp recently announced listings so the next watch pass posts them again."""
    # Same isoformat the stamp was written with, so the comparison stays lexicographic.
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    cleared = connection.execute(
        "UPDATE listings SET notified_at = NULL WHERE notified_at >= ?", (cutoff,)
    ).rowcount
    connection.commit()
    return cleared
