import re
import sqlite3

from depas.fetch import Fetcher
from depas.grade import Scale
from depas.metro import nearest_station
from depas.portals import portalinmobiliario
from depas.portals.portalinmobiliario import LISTING_HOSTS, clean_url
from depas.store import connect, save, save_detail
from depas.telegram import call, format_listing, send_listing

# Both hosts serve the same listings under the same MLC ids, so a link to either
# resolves to one row.
LISTING_LINK = re.compile(
    r"https?://[\w.-]*(?:" + "|".join(h.replace(".", r"\.") for h in LISTING_HOSTS) + r")/\S*?MLC-\d+\S*"
)
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


def _grade_link(connection: sqlite3.Connection, fetcher: Fetcher, url: str) -> tuple[dict, object] | None:
    """Return the stored row for a pasted link, fetching and enriching it if unseen."""
    listing_id = portalinmobiliario.LISTING_ID.search(url)
    if listing_id is None:
        return None
    key = (portalinmobiliario.NAME, listing_id.group(1))

    if connection.execute(
        "SELECT 1 FROM listings WHERE portal = ? AND external_id = ?", key
    ).fetchone() is None:
        listing = portalinmobiliario.fetch_standalone(fetcher, url)
        if listing is None:
            return None
        save(connection, [listing])

    row = connection.execute(
        "SELECT * FROM listings WHERE portal = ? AND external_id = ?", key
    ).fetchone()
    if row["detail_fetched_at"] is None:
        detail = portalinmobiliario.fetch_detail(fetcher, row["url"])
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
    links = LISTING_LINK.findall(message.get("text") or message.get("caption") or "")
    for url in dict.fromkeys(clean_url(link) for link in links):
        graded = _grade_link(connection, fetcher, url)
        if graded is None:
            continue
        row, grade = graded
        send_listing(str(message["chat"]["id"]), format_listing(row, grade), row.get("image_url"))


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
