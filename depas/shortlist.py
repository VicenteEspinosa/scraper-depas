"""The ⭐ set as one pinned message, rewritten whenever a verdict changes it."""
import sqlite3
from datetime import datetime

from depas.commute import SANTIAGO
from depas.grade import Scale
from depas.preferences import Preferences
from depas.store import LIKE, remember_shortlist, stored_shortlist
from depas.telegram import (
    GRADE_EMOJI,
    clp,
    edit_text,
    escape,
    message_link,
    pin,
    reply,
)

TITLE = "⭐ <b>Tu lista</b>"
EMPTY = ("⭐ <b>Tu lista</b>\n\nTodavía no marcaste ninguno. Aprieta <b>⭐ Me interesa</b> "
         "en cualquier tarjeta y aparecerá acá.")
# A list nobody can scan is not a shortlist, and Telegram rejects -- rather than trims --
# a message past its own limit, so the count is capped for the reader and the length for it.
MOST = 30
LIMIT = 4096
CARD_LABEL, LISTING_LABEL = "tarjeta", "aviso"


def starred(connection: sqlite3.Connection, prefs: Preferences) -> list[tuple[dict, object]]:
    """Every listing you marked interesting, best first, graded as it is graded today."""
    scale = Scale(prefs)
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM listings_ranked WHERE interest = ?", (LIKE,))]
    return sorted(((row, scale.grade(row)) for row in rows),
                  key=lambda pair: pair[1].score, reverse=True)


def _card_link(connection: sqlite3.Connection, row: dict, chat_id: str) -> str | None:
    """A deep link back to the card this listing was announced on, where there is one."""
    card = connection.execute(
        "SELECT message_id FROM card_messages WHERE chat_id = ? AND portal = ? "
        "AND external_id = ? ORDER BY posted_at DESC LIMIT 1",
        (str(chat_id), row["portal"], row["external_id"]),
    ).fetchone()
    return message_link(chat_id, card["message_id"]) if card else None


def _entry(connection: sqlite3.Connection, row: dict, grade: object, chat_id: str) -> str:
    """One line: what it is, what it costs, and the two ways back to it."""
    commune = (row.get("commune") or "").replace("-", " ").title()
    head = " · ".join(part for part in (
        f"{GRADE_EMOJI.get(grade.letter, '⚪')} <b>{grade.letter} {grade.score}</b>",
        escape(commune) or None,
        clp(row.get("net_monthly_clp")),
        f"{row['area']:.0f} m²" if row.get("area") else None,
    ) if part)
    # The card is where the buttons and the breakdown are, so it leads; the aviso is the
    # portal. A listing pasted into the chat was never announced and only has the second.
    card = _card_link(connection, row, chat_id)
    ways = [f'<a href="{card}">{CARD_LABEL}</a>'] if card else []
    ways.append(f'<a href="{escape(row["url"])}">{LISTING_LABEL}</a>')
    return f"{head}\n    {' · '.join(ways)} · <code>[{row['id']}]</code>"


def _more(left_out: int) -> str:
    return f"\n…y {left_out} más: <code>depas show</code> los muestra todos."


def format_shortlist(connection: sqlite3.Connection, prefs: Preferences,
                     chat_id: str) -> str:
    """Render the ⭐ set as the one message that is kept pinned, best first."""
    found = starred(connection, prefs)
    if not found:
        return EMPTY
    when = datetime.now(SANTIAGO).strftime("%d/%m %H:%M")
    header = f"{TITLE} · {len(found)} depto{'s' if len(found) != 1 else ''} · {when}"

    kept: list[str] = []
    # Budgeted against the longest footer it could end up needing, so adding one is
    # never what pushes the whole message past the limit and loses all of it.
    budget = LIMIT - len(header) - len(_more(len(found)))
    for row, grade in found[:MOST]:
        entry = _entry(connection, row, grade, chat_id)
        if budget - len(entry) - 1 < 0:
            break
        budget -= len(entry) + 1
        kept.append(entry)

    lines = [header, "", *kept]
    if len(found) > len(kept):
        lines.append(_more(len(found) - len(kept)))
    return "\n".join(lines)


def sync(connection: sqlite3.Connection, prefs: Preferences) -> bool:
    """Rewrite the pinned list in place, posting and pinning it the first time."""
    # Total on purpose: the list is a convenience, and a verdict is what actually matters.
    # Nothing that happens to it may cost the press or the command that triggered it.
    try:
        return _sync(connection, prefs)
    except (RuntimeError, ValueError) as error:
        print(f"could not update the pinned list: {error}")
        return False


def _sync(connection: sqlite3.Connection, prefs: Preferences) -> bool:
    chat_id = prefs.chat_id()
    text = format_shortlist(connection, prefs, chat_id)
    stored = stored_shortlist(connection)
    # A chat that has changed leaves the old message where it was: it is not ours to move.
    if stored and stored[0] == str(chat_id):
        # An edit that changes nothing is refused by Telegram, which is not a failure.
        edit_text(str(chat_id), stored[1], text)
        return True
    sent = reply(str(chat_id), text)
    # Remembered before it is pinned: pinning needs rights the bot may not have, and a
    # list that is only unpinned is still a list the next verdict can edit.
    remember_shortlist(connection, sent["chat"]["id"], sent["message_id"])
    try:
        pin(str(chat_id), sent["message_id"])
    except RuntimeError as error:
        print(f"could not pin the list, leaving it in the chat unpinned: {error}")
    return True
