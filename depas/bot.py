import re
import sqlite3
import time

from curl_cffi.requests.exceptions import RequestException

from depas import configure, shortlist
from depas.fetch import Fetcher
from depas.grade import Scale
from depas.home import row as home_row
from depas.metro import nearest_station
from depas.portals import PORTALS
from depas.portals.portalinmobiliario import clean_url
from depas.preferences import Preferences
from depas.store import (
    DISLIKE,
    LIKE,
    card_for_message,
    card_for_thread,
    connect,
    link_thread,
    remember_breakdown,
    remember_card,
    save,
    save_detail,
    set_interest,
)
from depas.telegram import (
    DISLIKE_BUTTON,
    LIKE_BUTTON,
    UNDO_BUTTON,
    answer_callback,
    call,
    edit_buttons,
    edit_listing,
    edit_text,
    format_breakdown,
    format_comparison,
    format_listing,
    reply,
    send_buttons,
    send_listing,
    verdict_buttons,
)
from depas.uf import normalize, stored_uf

POLL_TIMEOUT = 30
# A blip should cost one poll, not the process: the container restarts on exit.
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


def _grade_link(connection: sqlite3.Connection, fetcher: Fetcher, portal_name: str,
                url: str, prefs: Preferences) -> tuple[dict, object] | None:
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
    return dict(ranked), Scale(prefs).grade(dict(ranked))


COMMANDS = {"/like": LIKE, "/dislike": DISLIKE}
COMPARE = "/compare"
VERDICT = {LIKE: "⭐ anotado como interesante",
           DISLIKE: "🚫 descartado: no volverá a aparecer en las alertas"}
NO_CARD = ("no sé de qué depto hablas: comenta /like, /dislike o /compare en el hilo "
           "de una tarjeta, o respondiendo a una.")
NO_HOME = ("no sé dónde vives: configura DEPAS_CURRENT_HOME con el JSON de tu depto "
           "actual (`depas config get DEPAS_CURRENT_HOME` explica el formato) y "
           "vuelve a intentarlo.")
GONE = "ese aviso ya no está en la base"
# The id every card prints in its header, which is how an old card finds its listing.
CARD_ID = re.compile(r"\[(\d+)\]")


def _first_word(text: str) -> str:
    """The command a message opens with, stripped of the bot it is addressed to."""
    words = text.split()
    # /like@depas_bot is what Telegram sends when more than one bot is in the chat.
    return words[0].split("@")[0].lower() if words else ""


def _forwarded_from(message: dict) -> tuple[object, int] | None:
    """The channel post an auto-forwarded copy came from."""
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel":
        return origin["chat"]["id"], origin["message_id"]
    # The pre-7.0 spelling of the same thing, still what older clients send.
    if message.get("forward_from_chat"):
        return message["forward_from_chat"]["id"], message["forward_from_message_id"]
    return None


def _remember_forward(connection: sqlite3.Connection, message: dict,
                      prefs: Preferences) -> None:
    """Pair a card auto-forwarded into the discussion group with the post it copies."""
    origin = _forwarded_from(message)
    if origin is not None:
        link_thread(connection, *origin, message["chat"]["id"], message["message_id"])
        _offer_buttons(connection, *origin, message, prefs)


PRESS_PROMPT = "¿Qué te parece? (o comenta /compare para verlo contra tu depto)"


def _offer_buttons(connection: sqlite3.Connection, card_chat: object, card_message: int,
                   forward: dict, prefs: Preferences) -> None:
    """Open the card's thread with the verdict keyboard, then the grade it is judging."""
    card = card_for_message(connection, card_chat, card_message)
    if card is None:
        return  # not a card we posted: there is nothing to rate
    listing = connection.execute(
        "SELECT rowid AS id, interest FROM listings WHERE portal = ? AND external_id = ?",
        (card["portal"], card["external_id"]),
    ).fetchone()
    if listing is None:
        return
    try:
        send_buttons(str(forward["chat"]["id"]), PRESS_PROMPT, forward["message_id"],
                     verdict_buttons(listing["id"], listing["interest"]))
    except RuntimeError as error:
        # The thread is linked either way; the keyboard is the shortcut, not the feature.
        print(f"could not post the buttons in thread {forward['message_id']}: {error}")
    # After the keyboard, so the thread opens on the verdict and explains itself below it.
    post_breakdown(connection, dict(card), prefs)


def _from_card_text(connection: sqlite3.Connection, text: str) -> dict | None:
    """The listing a card is about, read back from the [id] its header prints."""
    # The header only: a bracketed number in a title would rate some unrelated listing.
    found = CARD_ID.search(text.split("\n")[0])
    if not found:
        return None
    row = connection.execute(
        "SELECT portal, external_id FROM listings WHERE rowid = ?", (int(found.group(1)),)
    ).fetchone()
    return dict(row) if row else None


def _card(connection: sqlite3.Connection, message: dict) -> dict | None:
    """The card a command refers to: its listing, and the message to edit if we posted it."""
    chat = str(message["chat"]["id"])
    thread = message.get("message_thread_id")
    row = card_for_thread(connection, chat, thread) if thread else None
    replied = message.get("reply_to_message") or {}
    if row is None and replied:
        row = card_for_message(connection, chat, replied["message_id"])
    if row is not None:
        return dict(row)
    return _from_card_text(connection, replied.get("text") or replied.get("caption") or "")


def refresh_card(connection: sqlite3.Connection, card: dict, prefs: Preferences) -> bool:
    """Re-render a card we posted, so the verdict shows on the card itself."""
    if not card.get("message_id"):
        return False  # traced back by its printed id alone; there is no message to edit
    key = (card["portal"], card["external_id"])
    row = connection.execute(
        "SELECT * FROM listings_ranked WHERE portal = ? AND external_id = ?", key
    ).fetchone()
    if row is None:
        return False  # a card outliving its listing must not take the bot down with it
    grade = Scale(prefs).grade(dict(row))
    try:
        # Offered on every redraw and withheld per chat where it would hide the comments.
        edit_listing(card["chat_id"], card["message_id"],
                     format_listing(dict(row), grade, prefs), bool(card["is_photo"]),
                     verdict_buttons(row["id"], row["interest"]))
    except RuntimeError as error:
        # Redrawing is a nicety: a card too old to edit must not cost the stored verdict.
        print(f"could not redraw card {card['message_id']}: {error}")
        return False
    finally:
        # Its own failures are already swallowed, and a card redrawn is a grade restated.
        refresh_breakdown(connection, card, prefs)
    return True


# A discarded card keeps only what says which listing it was, and so does its breakdown.
DISCARDED_BREAKDOWN = "🚫 descartado"


def _breakdown_anchor(card: dict) -> tuple[str, int]:
    """Where a breakdown hangs: the card's Comments thread, or the card itself."""
    # The same anchor the verdict keyboard uses, so the two always sit together.
    if card.get("thread_chat_id") and card.get("thread_id"):
        return str(card["thread_chat_id"]), int(card["thread_id"])
    return str(card["chat_id"]), int(card["message_id"])


def _breakdown_text(row: dict, prefs: Preferences) -> str:
    return (DISCARDED_BREAKDOWN if row.get("interest") == DISLIKE
            else format_breakdown(Scale(prefs).grade(row), prefs))


def _ranked(connection: sqlite3.Connection, card: dict) -> dict | None:
    """The card's listing as the ranked view sees it, or None once it is gone."""
    row = connection.execute(
        "SELECT * FROM listings_ranked WHERE portal = ? AND external_id = ?",
        (card["portal"], card["external_id"]),
    ).fetchone()
    return dict(row) if row else None


def post_breakdown(connection: sqlite3.Connection, card: dict, prefs: Preferences) -> bool:
    """Post the components behind a card's grade under it, beneath the verdict buttons."""
    if card.get("detail_message_id"):
        return False  # already explained; a second one would only be scrolled past
    row = _ranked(connection, card)
    if row is None:
        return False
    chat, anchor = _breakdown_anchor(card)
    try:
        sent = reply(chat, _breakdown_text(row, prefs), reply_to=anchor)
    except RuntimeError as error:
        # The breakdown is the footnote, not the alert: a card still stands without it.
        print(f"could not post the breakdown under {card['message_id']}: {error}")
        return False
    remember_breakdown(connection, card["chat_id"], card["message_id"],
                       sent["chat"]["id"], sent["message_id"])
    return True


def refresh_breakdown(connection: sqlite3.Connection, card: dict, prefs: Preferences) -> bool:
    """Re-render a breakdown already posted, so a verdict and today's rules show on it."""
    if not card.get("detail_message_id"):
        return False
    row = _ranked(connection, card)
    if row is None:
        return False
    try:
        edit_text(str(card["detail_chat_id"]), int(card["detail_message_id"]),
                       _breakdown_text(row, prefs))
    except RuntimeError as error:
        # Telegram refuses an edit that changes nothing, which is not a failure worth raising.
        print(f"could not redraw the breakdown {card['detail_message_id']}: {error}")
        return False
    return True


def _rate(connection: sqlite3.Connection, message: dict, interest: int,
          prefs: Preferences) -> None:
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
    refresh_card(connection, card, prefs)
    shortlist.sync(connection, prefs)
    reply(chat, VERDICT[interest], thread, message["message_id"])


def _compare(connection: sqlite3.Connection, fetcher: Fetcher, message: dict,
             prefs: Preferences) -> None:
    """Answer /compare with this card's listing set against the place you live in now."""
    chat = str(message["chat"]["id"])
    thread = message.get("message_thread_id")
    card = _card(connection, message)
    if card is None:
        reply(chat, NO_CARD, thread, message["message_id"])
        return
    home = home_row(connection, fetcher, prefs)
    if home is None:
        reply(chat, NO_HOME, thread, message["message_id"])
        return
    listing = connection.execute(
        "SELECT * FROM listings_ranked WHERE portal = ? AND external_id = ?",
        (card["portal"], card["external_id"]),
    ).fetchone()
    if listing is None:
        reply(chat, GONE, thread, message["message_id"])
        return
    scale = Scale(prefs)
    reply(chat, format_comparison(dict(listing), scale.grade(dict(listing)),
                                  home, scale.grade(home)),
          thread, message["message_id"])


BUTTONS = {LIKE_BUTTON: LIKE, DISLIKE_BUTTON: DISLIKE, UNDO_BUTTON: None}
TOAST = {LIKE: "⭐ anotado como interesante", DISLIKE: "🚫 descartado, no vuelve a aparecer",
         None: "↩️ veredicto deshecho, la tarjeta vuelve como estaba"}


def _pressed_card(connection: sqlite3.Connection, message: dict, listing: dict) -> dict:
    """The card to redraw after a press: the one pressed, or the card it hangs under."""
    card = card_for_message(connection, message["chat"]["id"], message["message_id"]) \
        if message else None
    if card is None and message.get("message_thread_id"):
        card = card_for_thread(connection, message["chat"]["id"],
                               message["message_thread_id"])
    if card is None:
        card = connection.execute(
            "SELECT * FROM card_messages WHERE portal = ? AND external_id = ? "
            "ORDER BY posted_at DESC LIMIT 1", (listing["portal"], listing["external_id"])
        ).fetchone()
    return dict(card) if card else {}


def _handle_callback(connection: sqlite3.Connection, callback: dict,
                     prefs: Preferences) -> None:
    """A button pressed on a card: the same verdict, with nothing typed and no reply posted."""
    if (callback.get("data") or "").startswith(configure.PREFIX):
        configure.press(connection, callback, prefs)
        return
    action, _, listing_id = (callback.get("data") or "").partition(":")
    if action not in BUTTONS or not listing_id.isdigit():
        answer_callback(callback["id"], "botón no reconocido")
        return
    interest = BUTTONS[action]
    listing = connection.execute(
        "SELECT portal, external_id FROM listings WHERE rowid = ?", (int(listing_id),)
    ).fetchone()
    if listing is None:
        answer_callback(callback["id"], GONE)
        return

    author = callback.get("from") or {}
    set_interest(connection, listing["portal"], listing["external_id"], interest,
                 author.get("username") or author.get("first_name"))
    # Acknowledged before the redraw: a press times out in about ten seconds.
    answer_callback(callback["id"], TOAST[interest])
    pressed = callback.get("message") or {}
    card = _pressed_card(connection, pressed, dict(listing))
    refresh_card(connection, card, prefs)
    shortlist.sync(connection, prefs)
    _tick(pressed, card, int(listing_id), interest)


def _tick(pressed: dict, card: dict, listing_id: int, interest: int) -> None:
    """Show the verdict on the keyboard that was pressed, when it is not the card itself."""
    if not pressed:
        return  # a press old enough that Telegram no longer sends the message
    if (str(pressed["chat"]["id"]), pressed["message_id"]) \
            == (str(card.get("chat_id")), card.get("message_id")):
        return  # the redraw already carried the ticked keyboard, or withheld it
    try:
        edit_buttons(str(pressed["chat"]["id"]), pressed["message_id"],
                     verdict_buttons(listing_id, interest))
    except RuntimeError as error:
        # The group's copy carries the channel's own keyboard, which is not ours to edit.
        print(f"could not tick the keyboard on {pressed['message_id']}: {error}")


def _handle(connection: sqlite3.Connection, fetcher: Fetcher, message: dict,
            prefs: Preferences) -> None:
    # Telegram's own copy of a channel card is bookkeeping, never a request.
    if message.get("is_automatic_forward"):
        _remember_forward(connection, message, prefs)
        return

    text = message.get("text") or message.get("caption") or ""
    command = _first_word(text)
    if command in COMMANDS:
        _rate(connection, message, COMMANDS[command], prefs)
        return
    if command == COMPARE:
        _compare(connection, fetcher, message, prefs)
        return
    if command in (configure.COMMAND, configure.START):
        configure.open_menu(connection, message, prefs)
        return
    # Read before the links below: an answer to a prompt was not posted to be graded.
    if configure.answer_prompt(connection, fetcher, message, prefs):
        return

    for portal_name, url in find_links(text):
        graded = _grade_link(connection, fetcher, portal_name, url, prefs)
        if graded is None:
            continue
        row, grade = graded
        sent = send_listing(str(message["chat"]["id"]), format_listing(row, grade, prefs),
                            row.get("image_url"), message.get("message_thread_id"),
                            verdict_buttons(row["id"], row.get("interest")))
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
            # Read once per poll, so a setting edited while the bot runs needs no restart.
            prefs = Preferences.load(connection)
            for update in updates:
                message = update.get("message") or update.get("channel_post")
                # A pressed button arrives on this same poll — no webhook, no open port.
                callback = update.get("callback_query")
                try:
                    if message:
                        _handle(connection, fetcher, message, prefs)
                    elif callback:
                        _handle_callback(connection, callback, prefs)
                except (RuntimeError, RequestException) as error:
                    print(f"could not answer update {update['update_id']}: {error}")
                # Advances even when the reply failed, or a bad update comes back forever.
                _remember_offset(connection, update["update_id"] + 1)
    finally:
        fetcher.close()
        connection.close()
