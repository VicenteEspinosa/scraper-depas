"""Browsing the pool from a private chat: one message that pages through it in place."""
import sqlite3

from depas.grade import Scale
from depas.preferences import Preferences
from depas.store import LIKE, pool_query, set_interest
from depas.telegram import (
    DISLIKE_BUTTON,
    LIKE_BUTTON,
    UNDO_BUTTON,
    UNDO_LABEL,
    answer_callback,
    edit_menu,
    format_listing,
    send_menu,
)

COMMAND = "/top"
# Its own namespace, so a press here is never read as a press on a card's keyboard.
PREFIX = "b:"
# Telegram caps callback_data at 64 bytes, so a screen is addressed rather than described:
# every press carries where to render next, and only RATE carries anything to do first.
# GO and FILTER differ in what the button means, not in what happens -- both just navigate.
GO, RATE, FILTER = "g", "r", "f"

POOL, STARRED = 0, 1
VIEWS = {POOL: "todos", STARRED: "⭐ marcados"}

PRIVATE_ONLY = ("esto es para el privado: escríbeme directo y mándame /top ahí.\n\n"
                "Acá en el grupo la lista fija ya muestra lo marcado.")
DENIED = "no estás en DEPAS_ADMINS"
NO_ADMINS = ("nadie puede navegar el pool todavía.\n\nTu id de Telegram es "
             "<code>{user_id}</code>: agrégalo con\n"
             "<code>depas config set DEPAS_ADMINS {user_id}</code>")
EMPTY = {POOL: ("No hay nada enriquecido para mostrar todavía. Corre <code>depas watch</code> "
                "o espera a la próxima pasada."),
         STARRED: "Todavía no marcaste ninguno. Aprieta ⭐ en cualquier tarjeta."}
STALE = "ese aviso ya no está en el pool"
STALE_KEYBOARD = "ese menú quedó viejo; manda /top otra vez"


def _listings(connection: sqlite3.Connection, prefs: Preferences,
              view: int) -> list[tuple[dict, object]]:
    """The pool as this view sees it, graded and best first."""
    # The same pool the alerts draw from, so browsing and alerting never disagree.
    query = pool_query(prefs)
    if view == STARRED:
        # Starred means starred: a listing you marked is shown even if the pool moved on.
        query = "SELECT * FROM listings_ranked WHERE interest = ?"
    rows = [dict(row) for row in connection.execute(
        query, (LIKE,) if view == STARRED else ())]
    scale = Scale(prefs)
    return sorted(((row, scale.grade(row)) for row in rows),
                  key=lambda pair: pair[1].score, reverse=True)


def _button(label: str, *parts: object) -> dict[str, str]:
    return {"text": label, "callback_data": PREFIX + ":".join(str(part) for part in parts)}


def _keyboard(index: int, view: int, found: list, listing_id: int) -> dict[str, object]:
    """Where to go, what to make of it, and which half of the pool is being shown."""
    last = len(found) - 1
    interest = found[index][0].get("interest")
    paging = [_button("◀️", GO, max(index - 1, 0), view) if index else None,
              _button(f"{index + 1}/{len(found)}", GO, index, view),
              _button("▶️", GO, min(index + 1, last), view) if index < last else None]
    if interest is not None:
        verdict = [_button(UNDO_LABEL[interest], RATE, index, view, listing_id, UNDO_BUTTON)]
    else:
        verdict = [_button("⭐ Me interesa", RATE, index, view, listing_id, LIKE_BUTTON),
                   _button("🚫 Descartar", RATE, index, view, listing_id, DISLIKE_BUTTON)]
    # Switching view starts over: the position in one ordering means nothing in the other.
    other = STARRED if view == POOL else POOL
    return {"inline_keyboard": [[button for button in paging if button], verdict,
                                [_button(f"🔀 Ver: {VIEWS[other]}", FILTER, 0, other)]]}


def screen(connection: sqlite3.Connection, prefs: Preferences, index: int,
           view: int) -> tuple[str, dict[str, object] | None]:
    """One listing of the pool, rendered as the card it would have been posted as."""
    found = _listings(connection, prefs, view)
    if not found:
        return EMPTY[view], None
    index = max(0, min(index, len(found) - 1))
    row, grade = found[index]
    header = f"🔎 <b>{VIEWS[view]}</b> · {index + 1} de {len(found)}"
    return f"{header}\n\n{format_listing(row, grade, prefs)}", _keyboard(
        index, view, found, row["id"])


def _author(message: dict) -> int | None:
    """Who sent this, or None -- a channel post is signed by the channel, not a person."""
    return (message.get("from") or {}).get("id")


def open_browser(connection: sqlite3.Connection, message: dict, prefs: Preferences) -> None:
    """Answer /top with the first screen, or with why this sender cannot have it."""
    chat = str(message["chat"]["id"])
    if message["chat"].get("type") != "private":
        send_menu(chat, PRIVATE_ONLY, None, message.get("message_thread_id"),
                  message["message_id"])
        return
    user_id = _author(message)
    if not prefs.is_admin(user_id):
        # Telling somebody their own id is the whole bootstrap: it is what they paste in.
        send_menu(chat, NO_ADMINS.format(user_id=user_id), None, None, message["message_id"])
        return
    text, keyboard = screen(connection, prefs, 0, POOL)
    send_menu(chat, text, keyboard, None, message["message_id"])


TOAST = {LIKE_BUTTON: "⭐ anotado", DISLIKE_BUTTON: "🚫 descartado",
         UNDO_BUTTON: "↩️ deshecho"}
INTEREST = {LIKE_BUTTON: 1, DISLIKE_BUTTON: -1, UNDO_BUTTON: None}


NO_KEYBOARD: dict[str, object] = {"inline_keyboard": []}


def _parsed(data: str) -> tuple[str, int, int, int, str] | None:
    """What a button carries, or None for a keyboard whose encoding we no longer speak."""
    action, *rest = data.removeprefix(PREFIX).split(":")
    try:
        index, view = int(rest[0]), int(rest[1])
        # Navigation carries no listing; only RATE has anything to do before rendering.
        listing_id, button = (int(rest[2]), rest[3]) if action == RATE else (0, "")
    except (IndexError, ValueError):
        return None
    if view not in VIEWS or (action == RATE and button not in INTEREST):
        return None
    return action, index, view, listing_id, button


def press(connection: sqlite3.Connection, callback: dict,
          prefs: Preferences) -> int | None:
    """One press on the browser's keyboard, and the listing it rated, if it rated one.

    Authorised against the whitelist every time: in a private chat the sender is the
    only person who could press, but the whitelist can be edited between two presses."""
    if not prefs.is_admin(_author(callback)):
        answer_callback(callback["id"], DENIED)
        return None
    parsed = _parsed(callback.get("data") or "")
    if parsed is None:
        # A keyboard from before a deploy changed the encoding; saying so beats a traceback.
        answer_callback(callback["id"], STALE_KEYBOARD)
        return None
    action, index, view, listing_id, button = parsed

    rated, toast = None, ""
    if action == RATE:
        author = callback.get("from") or {}
        rated, toast = listing_id, _rate(connection, listing_id, button,
                                         author.get("username") or author.get("first_name"))
    # Acknowledged before the redraw: a press times out in about ten seconds.
    answer_callback(callback["id"], toast)

    message = callback.get("message") or {}
    if message:
        text, keyboard = screen(connection, prefs, index, view)
        edit_menu(str(message["chat"]["id"]), message["message_id"], text,
                  keyboard or NO_KEYBOARD)
    return rated


def _rate(connection: sqlite3.Connection, listing_id: int, button: str,
          rated_by: str | None) -> str:
    """Record a verdict given while browsing, on the listing the screen was showing."""
    listing = connection.execute(
        "SELECT portal, external_id FROM listings WHERE rowid = ?", (listing_id,)
    ).fetchone()
    if listing is None:
        return STALE
    # The same column a card's buttons write, so a verdict means the same either way.
    set_interest(connection, listing["portal"], listing["external_id"],
                 INTEREST[button], rated_by)
    return TOAST[button]
