import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from depas.config import lease_income
from depas.detail import DETAIL_COLUMNS
from depas.models import Listing

DB_PATH = Path("depas.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    portal          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    price           REAL NOT NULL,
    currency        TEXT NOT NULL,
    common_expenses INTEGER,
    is_project      INTEGER NOT NULL DEFAULT 0,
    price_clp       REAL,
    bedrooms        INTEGER,
    bathrooms       INTEGER,
    area_m2         REAL,
    commune         TEXT,
    address         TEXT,
    lat             REAL,
    lon             REAL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    PRIMARY KEY (portal, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    seen_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing
    ON price_history (portal, external_id, seen_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('parking_income', 0), ('storage_income', 0);
"""

FIELDS = (
    "url", "title", "price", "currency", "common_expenses", "is_project", "price_clp",
    "bedrooms", "bathrooms", "area_m2", "commune", "address", "lat", "lon",
)


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    _add_detail_columns(connection)
    connection.executescript(RANKED_VIEW)
    _sync_lease_income(connection)
    return connection


RANKED_VIEW = """
DROP VIEW IF EXISTS listings_ranked;
CREATE VIEW listings_ranked AS
SELECT *,
       COALESCE(area_useful_m2, area_m2)        AS area,
       price_clp + COALESCE(common_expenses, 0) AS total_monthly_clp,
       price_clp + COALESCE(common_expenses, 0)
           - COALESCE(parking_spaces, 0) * (SELECT value FROM settings WHERE key = 'parking_income')
           - COALESCE(storage_units, 0)  * (SELECT value FROM settings WHERE key = 'storage_income')
                                                AS net_monthly_clp
FROM listings;
"""


def _sync_lease_income(connection: sqlite3.Connection) -> None:
    """Mirror the environment into `settings` so the ranked view can read it from SQL."""
    for kind in ("parking", "storage"):
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?", (lease_income(kind), f"{kind}_income")
        )
    connection.commit()


def _add_detail_columns(connection: sqlite3.Connection) -> None:
    """Detail columns are added in place so enriching never costs the existing price history."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(listings)")}
    for column, sql_type in DETAIL_COLUMNS.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE listings ADD COLUMN {column} {sql_type}")
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
