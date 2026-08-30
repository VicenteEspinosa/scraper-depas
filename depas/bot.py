import re
import sqlite3
import time

from curl_cffi.requests.exceptions import RequestException

from depas.fetch import Fetcher
from depas.grade import Scale
from depas.metro import nearest_station
from depas.portals import PORTALS
from depas.portals.portalinmobiliario import clean_url
from depas.store import (DISLIKE, LIKE, POOL_QUERY, card_for_message, card_for_thread,
                         connect, link_thread, remember_card, save, save_detail,
                         set_interest)
from depas.uf import normalize, stored_uf
from depas.telegram import call, edit_listing, format_listing, reply, send_listing

POLL_TIMEOUT = 30
# Telegram and the portals both blip. A blip should cost one poll, not the process:
# the container restarts on exit, so crashing is how a hiccup became a restart loop.
ERROR_BACKOFF_SECONDS = 5
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
    pool = connection.execute(POOL_QUERY).fetchall()
    return dict(ranked), Scale([dict(item) for item in pool]).grade(dict(ranked))


COMMANDS = {"/like": LIKE, "/dislike": DISLIKE}
VERDICT = {LIKE: "⭐ anotado como interesante",
           DISLIKE: "🚫 descartado: no volverá a aparecer en las alertas"}
NO_CARD = ("no sé de qué depto hablas: comenta /like o /dislike en el hilo de una "
           "tarjeta, o respondiendo a una.")
# The id every card prints in its header, which is how a card posted before the
# bot started recording them can still be traced back to its listing.
CARD_ID = re.compile(r"\[(\d+)\]")


def _command(text: str) -> int | None:
    """The verdict a message asks for, if its first word is one of ours."""
    words = text.split()
    if not words:
        return None
    # /like@depas_bot is what Telegram sends when more than one bot is in the chat.
    return COMMANDS.get(words[0].split("@")[0].lower())


def _forwarded_from(message: dict) -> tuple[object, int] | None:
    """The channel post an auto-forwarded copy came from."""
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel":
        return origin["chat"]["id"], origin["message_id"]
    # The pre-7.0 spelling of the same thing, still what older clients send.
    if message.get("forward_from_chat"):
        return message["forward_from_chat"]["id"], message["forward_from_message_id"]
    return None


def _remember_forward(connection: sqlite3.Connection, message: dict) -> None:
    """Pair a card auto-forwarded into the discussion group with the post it copies.

    Telegram publishes that pairing in this update and nowhere else: the copy's own
    message id is the message_thread_id every comment on the card will carry.
    """
    origin = _forwarded_from(message)
    if origin is not None:
        link_thread(connection, *origin, message["chat"]["id"], message["message_id"])


def _from_card_text(connection: sqlite3.Connection, text: str) -> dict | None:
    """The listing a card is about, read back from the [id] its header prints."""
    # The header only, not the whole card: a bracketed number anywhere in a title
    # or a description would otherwise rate some unrelated listing.
    found = CARD_ID.search(text.split("\n")[0])
    if not found:
        return None
    row = connection.execute(
        "SELECT portal, external_id FROM listings WHERE rowid = ?", (int(found.group(1)),)
    ).fetchone()
    return dict(row) if row else None


def _card(connection: sqlite3.Connection, message: dict) -> dict | None:
    """The card a command refers to: its listing, and the message to edit if we posted it.

    A comment in a channel's Comments names the thread it hangs off; a reply in a
    plain group only names the message it answers. Reading the [id] out of that
    message covers what neither does: cards posted before this was recorded.
    """
    chat = str(message["chat"]["id"])
    thread = message.get("message_thread_id")
    row = card_for_thread(connection, chat, thread) if thread else None
    replied = message.get("reply_to_message") or {}
    if row is None and replied:
        row = card_for_message(connection, chat, replied["message_id"])
    if row is not None:
        return dict(row)
    return _from_card_text(connection, replied.get("text") or replied.get("caption") or "")


def _refresh_card(connection: sqlite3.Connection, card: dict) -> None:
    """Re-render a card we posted, so the verdict shows on the card itself."""
    if not card.get("message_id"):
        return  # traced back by its printed id alone; there is no message to edit
    key = (card["portal"], card["external_id"])
    row = connection.execute(
        "SELECT * FROM listings_ranked WHERE portal = ? AND external_id = ?", key
    ).fetchone()
    if row is None:
        return  # a card outliving its listing must not take the bot down with it
    pool = connection.execute(POOL_QUERY).fetchall()
    grade = Scale([dict(item) for item in pool]).grade(dict(row))
    try:
        edit_listing(card["chat_id"], card["message_id"], format_listing(dict(row), grade),
                     bool(card["is_photo"]))
    except RuntimeError as error:
        # Redrawing the card is a nicety: a card too old to edit must not cost the
        # verdict, which is already stored and already filtering the alerts.
        print(f"could not redraw card {card['message_id']}: {error}")


def _rate(connection: sqlite3.Connection, message: dict, interest: int) -> None:
    """Record a /like or /dislike against the listing whose thread it was left in."""
    chat = str(message["chat"]["id"])
    thread = message.get("message_thread_id")
    card = _card(connection, message)
    if card is None:
        reply(chat, NO_CARD, thread, message["message_id"])
        return
    author = message.get("from") or {}
    set_interest(connection, card["portal"], card["external_id"], interest,
                 author.get("username") or author.get("first_name"))
    _refresh_card(connection, card)
    reply(chat, VERDICT[interest], thread, message["message_id"])


def _handle(connection: sqlite3.Connection, fetcher: Fetcher, message: dict) -> None:
    # A channel card is copied into the discussion group by Telegram itself. That
    # copy is bookkeeping, never a request: answering it would post the card twice.
    if message.get("is_automatic_forward"):
        _remember_forward(connection, message)
        return

    text = message.get("text") or message.get("caption") or ""
    interest = _command(text)
    if interest is not None:
        _rate(connection, message, interest)
        return

    for portal_name, url in find_links(text):
        graded = _grade_link(connection, fetcher, portal_name, url)
        if graded is None:
            continue
        row, grade = graded
        sent = send_listing(str(message["chat"]["id"]), format_listing(row, grade),
                            row.get("image_url"), message.get("message_thread_id"))
        remember_card(connection, sent["chat"]["id"], sent["message_id"],
                      row["portal"], row["external_id"], "photo" in sent)


def run() -> None:
    """Long-poll for messages and reply to any portal link with its graded card."""
    connection = connect()
    fetcher = Fetcher()
    stored_uf(connection, fetcher)  # the ranked view prices per m2 straight from this
    print("bot listening")
    try:
        while True:
            try:
                updates = call("getUpdates", offset=_offset(connection), timeout=POLL_TIMEOUT)
            except (RuntimeError, RequestException) as error:
                print(f"getUpdates failed, polling again: {error}")
                time.sleep(ERROR_BACKOFF_SECONDS)
                continue
            for update in updates:
                message = update.get("message") or update.get("channel_post")
                if message:
                    try:
                        _handle(connection, fetcher, message)
                    except (RuntimeError, RequestException) as error:
                        print(f"could not answer update {update['update_id']}: {error}")
                # Advances even when the reply failed: an update that cannot be
                # answered must not be redelivered on every restart forever.
                _remember_offset(connection, update["update_id"] + 1)
    finally:
        fetcher.close()
        connection.close()
