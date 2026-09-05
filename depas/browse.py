"""Browsing the pool from a private chat: one message that pages through it in place."""
import sqlite3
from dataclasses import dataclass, replace

from depas.grade import Scale
from depas.preferences import Preferences
from depas.store import LIKE, pool_query, set_interest
from depas.telegram import (
    DISLIKE_BUTTON,
    LIKE_BUTTON,
    UNDO_BUTTON,
    UNDO_LABEL,
    answer_callback,
    clp,
    edit_menu,
    escape,
    format_listing,
    price_change,
    send_menu,
)

COMMAND = "/top"
# Its own namespace, so a press here is never read as a press on a card's keyboard.
PREFIX = "b:"
# Telegram caps callback_data at 64 bytes, so a screen is addressed rather than described:
# every press carries where to render next, and only RATE carries anything to do first.
# The four navigating actions differ in what the button means, not in what happens.
GO, RATE, FILTER, SORT, MODE = "g", "r", "f", "s", "m"
NAVIGATE = (GO, FILTER, SORT, MODE)

POOL, STARRED = 0, 1
VIEWS = {POOL: "todos", STARRED: "⭐ marcados"}

CARD, LIST = 0, 1
MODES = {CARD: "🃏 fichas", LIST: "📋 lista"}
# How many listings one screen of the list shows. Ten fit two rows of jump buttons and
# still leave the message short enough to read without scrolling past it.
PAGE = 10


@dataclass(frozen=True, slots=True)
class Order:
    """One way of reading the pool: what it is called, and the key it sorts rows on."""

    label: str
    column: str
    descending: bool


# Ordering is the whole point of a pool: the same twenty flats are a different shortlist
# read cheapest-first than read by grade. `id` breaks every tie, so a screen is stable.
ORDERS = (
    Order("nota", "score", True),
    Order("precio", "net_monthly_clp", False),
    Order("metraje", "area", True),
    Order("metro", "walk_minutes", False),
    Order("nuevos", "first_seen", True),
)

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


@dataclass(frozen=True, slots=True)
class Where:
    """Everything a press carries. Nothing about a browse is stored between two of them,
    so the button is the state: a keyboard left open across a restart still works."""

    index: int = 0
    view: int = POOL
    order: int = 0
    mode: int = CARD

    def encode(self, action: str, *extra: object) -> str:
        return PREFIX + ":".join(
            str(part) for part in (action, self.index, self.view, self.order, self.mode,
                                   *extra))


def _sorted(found: list[tuple[dict, object]], order: Order) -> list[tuple[dict, object]]:
    """Rank the pool the way this screen asks for, with what nobody published last."""
    def value(pair: tuple[dict, object]) -> object:
        row, grade = pair
        return grade.score if order.column == "score" else row.get(order.column)

    # Split rather than sorted(key=...): a missing figure is unknown, which is neither the
    # best nor the worst, and `first_seen` is a string that no unary minus would reverse.
    known = sorted((pair for pair in found if value(pair) is not None),
                   key=lambda pair: (value(pair), pair[0]["id"]), reverse=order.descending)
    unknown = sorted((pair for pair in found if value(pair) is None),
                     key=lambda pair: pair[0]["id"])
    return known + unknown


def _listings(connection: sqlite3.Connection, prefs: Preferences,
              where: Where) -> list[tuple[dict, object]]:
    """The pool as this screen sees it: one view of it, in one order."""
    # The same pool the alerts draw from, so browsing and alerting never disagree.
    query = pool_query(prefs)
    if where.view == STARRED:
        # Starred means starred: a listing you marked is shown even if the pool moved on.
        query = "SELECT * FROM listings_ranked WHERE interest = ?"
    rows = [dict(row) for row in connection.execute(
        query, (LIKE,) if where.view == STARRED else ())]
    scale = Scale(prefs)
    return _sorted([(row, scale.grade(row)) for row in rows], ORDERS[where.order])


def _button(label: str, where: Where, action: str, *extra: object) -> dict[str, str]:
    return {"text": label, "callback_data": where.encode(action, *extra)}


def _verdict_row(where: Where, row: dict) -> list[dict[str, str]]:
    """The two verdicts, or the way back from the one already given."""
    interest = row.get("interest")
    if interest is not None:
        return [_button(UNDO_LABEL[interest], where, RATE, row["id"], UNDO_BUTTON)]
    return [_button("⭐ Me interesa", where, RATE, row["id"], LIKE_BUTTON),
            _button("🚫 Descartar", where, RATE, row["id"], DISLIKE_BUTTON)]


def _switches(where: Where) -> list[list[dict[str, str]]]:
    """The three things every screen can change about itself, whichever it is showing."""
    # Switching the view starts over: a position in one ordering means nothing in the other.
    other_view = STARRED if where.view == POOL else POOL
    other_mode = LIST if where.mode == CARD else CARD
    # Re-ordering starts over too: position 7 of one ranking is nowhere in the next.
    following = (where.order + 1) % len(ORDERS)
    return [[_button(f"↕️ Orden: {ORDERS[following].label}",
                     replace(where, index=0, order=following), SORT),
             _button(f"Ver {MODES[other_mode]}", replace(where, mode=other_mode), MODE)],
            [_button(f"🔀 Ver: {VIEWS[other_view]}",
                     replace(where, index=0, view=other_view), FILTER)]]


def _paging(where: Where, count: int, step: int, label: str) -> list[dict[str, str]]:
    """Back, where you are, and forward — by one listing, or by a screenful of them."""
    last = count - 1
    at = [_button("◀️", replace(where, index=max(where.index - step, 0)), GO)
          if where.index else None,
          _button(label, where, GO),
          _button("▶️", replace(where, index=min(where.index + step, last)), GO)
          if where.index + step <= last else None]
    return [button for button in at if button]


def _card_screen(found: list[tuple[dict, object]], where: Where,
                 prefs: Preferences) -> tuple[str, dict[str, object]]:
    """One listing, rendered as the card it would have been posted as."""
    row, grade = found[where.index]
    header = (f"🔎 <b>{VIEWS[where.view]}</b> · {where.index + 1} de {len(found)}"
              f" · por {ORDERS[where.order].label}")
    keyboard = [_paging(where, len(found), 1, f"{where.index + 1}/{len(found)}"),
                _verdict_row(where, row), *_switches(where)]
    return (f"{header}\n\n{format_listing(row, grade, prefs)}",
            {"inline_keyboard": keyboard})


def _marks(row: dict) -> str:
    """Two columns of news: whether you starred it, and whether its price has moved.

    Both, rather than whichever came first: in the ⭐ view a star says nothing and a
    markdown is the whole point, and in the pool the star is what stops you re-reading it."""
    change = price_change(row)
    return ("*" if row.get("interest") == LIKE else " ") + (
        "" if change is None else "↓" if change[0] < 0 else "↑")


# Which way each of the seven cells below is read: figures against their right edge,
# words against their left.
ALIGNMENT = (">", "<", "<", ">", ">", ">", "<")
# A commune longer than this is cut rather than allowed to push the figures out of line.
COMMUNE_LIMIT = 13
LEGEND = "* marcado · ↓ ↑ cambió de precio · ′ minutos al metro"


def _cells(number: int, row: dict, grade: object) -> tuple[str, ...]:
    """One listing as the cells of a row: what a pool is scanned on, and nothing else."""
    commune = (row.get("commune") or "").replace("-", " ").title()
    return (f"{number}.", f"{grade.letter} {grade.score}", commune[:COMMUNE_LIMIT],
            clp(row.get("net_monthly_clp")),
            f"{row['area']:.0f}m²" if row.get("area") else "—",
            f"{row['walk_minutes']}′" if row.get("walk_minutes") is not None else "",
            _marks(row))


def _table(page: list[tuple[dict, object]], start: int) -> str:
    """The rows padded to the widest cell in each column, so the eye can run down one.

    Measured over the page rather than fixed, because a single $1.030.000 among nine
    six-figure rents would otherwise shift every column to its right by one."""
    rows = [_cells(start + offset + 1, row, grade)
            for offset, (row, grade) in enumerate(page)]
    widths = [max(len(cell) for cell in column) for column in zip(*rows, strict=True)]
    return "\n".join(
        " ".join(cell.rjust(width) if side == ">" else cell.ljust(width)
                 for cell, width, side in zip(cells, widths, ALIGNMENT, strict=True)).rstrip()
        for cells in rows)


def _list_screen(found: list[tuple[dict, object]], where: Where) -> tuple[str, dict]:
    """A screenful of the pool at once, with a button straight to any line of it."""
    start = where.index - where.index % PAGE
    # Normalised to the page it is showing, so ◀️ and ▶️ move by a screenful from here.
    where = replace(where, index=start)
    page = found[start:start + PAGE]
    header = (f"📋 <b>{VIEWS[where.view]}</b> · {start + 1}-{start + len(page)} "
              f"de {len(found)} · por {ORDERS[where.order].label}")
    table = escape(_table(page, start))
    # Jump buttons rather than links: the card is a screen of this same message, and a
    # <pre> block is what makes the columns line up, which no link survives inside.
    jumps = [[_button(str(start + offset + 1), replace(where, index=start + offset,
                                                       mode=CARD), GO)
              for offset, _ in enumerate(page[half:half + PAGE // 2], start=half)]
             for half in (0, PAGE // 2)]
    keyboard = [*[row for row in jumps if row],
                _paging(where, len(found), PAGE,
                        f"{start // PAGE + 1}/{(len(found) - 1) // PAGE + 1}"),
                *_switches(where)]
    return (f"{header}\n\n<pre>{table}</pre>\n<i>{LEGEND}</i>",
            {"inline_keyboard": keyboard})


def screen(connection: sqlite3.Connection, prefs: Preferences,
           where: Where) -> tuple[str, dict[str, object] | None]:
    """Whatever this press asked to see, and the keyboard that moves on from it."""
    found = _listings(connection, prefs, where)
    if not found:
        return EMPTY[where.view], None
    # An index into a pool that has since shrunk lands on the last listing, not an error.
    where = replace(where, index=max(0, min(where.index, len(found) - 1)))
    if where.mode == LIST:
        return _list_screen(found, where)
    return _card_screen(found, where, prefs)


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
    # Opening on the list: the first question is what there is, not what the best one says.
    text, keyboard = screen(connection, prefs, Where(mode=LIST))
    send_menu(chat, text, keyboard, None, message["message_id"])


TOAST = {LIKE_BUTTON: "⭐ anotado", DISLIKE_BUTTON: "🚫 descartado",
         UNDO_BUTTON: "↩️ deshecho"}
INTEREST = {LIKE_BUTTON: 1, DISLIKE_BUTTON: -1, UNDO_BUTTON: None}


NO_KEYBOARD: dict[str, object] = {"inline_keyboard": []}


def _parsed(data: str) -> tuple[str, Where, int, str] | None:
    """What a button carries, or None for a keyboard whose encoding we no longer speak."""
    action, *rest = data.removeprefix(PREFIX).split(":")
    try:
        where = Where(int(rest[0]), int(rest[1]), int(rest[2]), int(rest[3]))
        # Navigation carries no listing; only RATE has anything to do before rendering.
        listing_id, button = (int(rest[4]), rest[5]) if action == RATE else (0, "")
    except (IndexError, ValueError):
        return None
    if where.view not in VIEWS or where.mode not in MODES or where.order >= len(ORDERS):
        return None
    if action == RATE and button not in INTEREST:
        return None
    if action not in NAVIGATE and action != RATE:
        return None
    return action, where, listing_id, button


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
    action, where, listing_id, button = parsed

    rated, toast = None, ""
    if action == RATE:
        author = callback.get("from") or {}
        rated, toast = listing_id, _rate(connection, listing_id, button,
                                         author.get("username") or author.get("first_name"))
    # Acknowledged before the redraw: a press times out in about ten seconds.
    answer_callback(callback["id"], toast)

    message = callback.get("message") or {}
    if message:
        text, keyboard = screen(connection, prefs, where)
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
