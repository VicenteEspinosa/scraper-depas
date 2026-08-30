import re
import sqlite3

from depas.fetch import Fetcher
from depas.grade import Scale
from depas.metro import nearest_station
from depas.portals import PORTALS
from depas.portals.portalinmobiliario import clean_url
from depas.store import connect, save, save_detail
from depas.uf import normalize
from depas.telegram import call, format_listing, send_listing

POLL_TIMEOUT = 30
OFFSET_KEY = "telegram_offset"


def _offset(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (OFFSET_KEY,)).fetchone()
    return row["value"] if row else 0


def _remember_offset(connection: sqlite3.Connection, offset: int) -> None:
    connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (OFFSET_KEY, offset),
    )
    connection.commit()


def find_links(text: str) -> list[tuple[str, str]]:
    """Every recognised listing link in a message, as (portal name, canonical url)."""
    # finditer, not findall: a pattern with a capture group would yield the group.
    found = [(name, clean_url(match.group(0)))
             for name, portal in PORTALS.items()
             for match in portal.LISTING_URL.finditer(text)]
    return list(dict.fromkeys(found))


def _grade_link(connection: sqlite3.Connection, fetcher: Fetcher,
                portal_name: str, url: str) -> tuple[dict, object] | None:
    """Return the stored row for a pasted link, fetching and enriching it if unseen."""
    portal = PORTALS[portal_name]
    identifier = portal.listing_id(url)
    if identifier is None:
        return None
    key = (portal_name, identifier)

    if connection.execute(
        "SELECT 1 FROM listings WHERE portal = ? AND external_id = ?", key
    ).fetchone() is None:
        listing = portal.fetch_standalone(fetcher, url)
        if listing is None:
            return None
        save(connection, [normalize(listing, fetcher)])

    row = connection.execute(
        "SELECT * FROM listings WHERE portal = ? AND external_id = ?", key
    ).fetchone()
    if row["detail_fetched_at"] is None:
        detail = portal.fetch_detail(fetcher, row["url"])
        if "nearest_station" not in detail and detail.get("lat") is not None:
            station, metres, minutes = nearest_station(detail["lat"], detail["lon"])
            detail |= {"nearest_station": station, "station_distance_m": metres,
                       "walk_minutes": minutes, "walk_source": "computed"}
        save_detail(connection, *key, detail)

    ranked = connection.execute(
        "SELECT * FROM listings_ranked WHERE portal = ? AND external_id = ?", key
    ).fetchone()
    pool = connection.execute(
        "SELECT * FROM listings_ranked WHERE detail_fetched_at IS NOT NULL AND is_project = 0"
    ).fetchall()
    return dict(ranked), Scale([dict(item) for item in pool]).grade(dict(ranked))


def _handle(connection: sqlite3.Connection, fetcher: Fetcher, message: dict) -> None:
    text = message.get("text") or message.get("caption") or ""
    for portal_name, url in find_links(text):
        graded = _grade_link(connection, fetcher, portal_name, url)
        if graded is None:
            continue
        row, grade = graded
        send_listing(str(message["chat"]["id"]), format_listing(row, grade), row.get("image_url"),
                     message.get("message_thread_id"))


def run() -> None:
    """Long-poll for messages and reply to any portal link with its graded card."""
    connection = connect()
    fetcher = Fetcher()
    print("bot listening")
    try:
        while True:
            updates = call("getUpdates", offset=_offset(connection), timeout=POLL_TIMEOUT)
            for update in updates:
                message = update.get("message") or update.get("channel_post")
                if message:
                    _handle(connection, fetcher, message)
                _remember_offset(connection, update["update_id"] + 1)
    finally:
        fetcher.close()
        connection.close()
