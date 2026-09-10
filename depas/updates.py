"""What changed about a listing since a chat last heard about it, and how it gets told.

Two different messages come out of the same reading, because a change means two different
things depending on whether the reader has seen the flat already:

  * **A card already posted** gets edited in place, its thread gets the diff, and one
    digest per pass names every listing that moved with a link back to its own card. One
    message rather than one per listing: a rebaja is worth knowing about, and ten
    separate notifications about ten of them is how a chat gets muted.
  * **A card about to be posted for the first time** carries the diff as the answer to
    the question the reader would otherwise ask — why is a flat first seen three weeks
    ago arriving now. Announcing is gated on requirements the *listing* can cross on its
    own (a rebaja, a gasto común finally published, a walk that got computed), so the
    honest answer is usually not "your criteria changed" but "this did".

The module owns its own messages, the way `shortlist` does: the rendering lives with the
reading that produces it, and `announce` only says when.
"""
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from depas.bot import card_anchor, refresh_card
from depas.commute import SANTIAGO
from depas.commute import as_text as commute_text
from depas.grade import Scale
from depas.preferences import Preferences
from depas.shortlist import CARD_LABEL, LISTING_LABEL
from depas.store import (
    GONE_404,
    GONE_UNSEEN,
    Subscriber,
    listing_events,
    mark_updates_reported,
)
from depas.telegram import GRADE_EMOJI, clp, escape, message_link, reply

# ── what counts as a change ─────────────────────────────────────────────────────

# Recorded on every re-read, and about the clock rather than the flat: a listing sitting
# untouched for a month has its `published_days_ago` move by thirty and its label with
# it, so counting them would make every re-read look like news. `zone_price_per_m2_uf`
# moves with the comuna's median, which is the neighbourhood changing, not the apartment.
NOT_A_CHANGE = frozenset({
    "published_days_ago", "published_label", "zone_price_per_m2_uf",
    # Bookkeeping of the reading itself, which the reader has no use for at all.
    "detail_fetched_at", "detail_hash", "detail_due_at", "detail_unchanged_count",
    "inferred_version", "price_at_detail", "last_seen",
})

# How a moved field is said in Spanish, and how its value is written. A field with no
# entry here is still reported — the name in code beats silence — but every field the
# portals actually publish should have one.
LABELS: dict[str, str] = {
    "price": "El arriendo", "price_clp": "El arriendo", "common_expenses": "El gasto común",
    "area_total_m2": "La superficie total", "area_useful_m2": "La superficie útil",
    "area_m2": "La superficie", "terrace_m2": "La terraza",
    "bedrooms": "Los dormitorios", "bathrooms": "Los baños", "rooms": "Los ambientes",
    "parking_spaces": "Los estacionamientos", "storage_units": "Las bodegas",
    "floor": "El piso", "building_floors": "Los pisos del edificio",
    "units_per_floor": "Los deptos por piso", "age_years": "La antigüedad",
    "orientation": "La orientación", "available_from": "La entrega",
    "furnished": "El amoblado", "pets_allowed": "Las mascotas",
    "has_elevator": "El ascensor", "has_concierge": "La conserjería",
    "security_type": "El tipo de seguridad", "gated_community": "El condominio cerrado",
    "has_heating": "La calefacción", "has_air_conditioning": "El aire acondicionado",
    "has_pool": "La piscina", "has_gym": "El gimnasio", "has_terrace": "La terraza",
    "description": "La descripción", "features": "Las características",
    "title": "El título", "url": "El enlace", "image_url": "La foto",
    "broker": "El corredor", "commune": "La comuna", "address": "La dirección",
    "commute": "El viaje", "transit": "El transporte", "nearest_station": "La estación",
    "station_distance_m": "La distancia a la estación", "walk_minutes": "La caminata",
    "walk_source": "De dónde sale la caminata", "price_per_m2_uf": "El UF/m²",
    "lat": "La ubicación", "lon": "La ubicación", "is_project": "El tipo de aviso",
}

# Fields written as money, as a count of minutes, and as square metres. Everything else
# is rendered as it was stored, which is right for a text and harmless for a number.
MONEY = frozenset({"price", "price_clp", "common_expenses"})
MINUTES = frozenset({"walk_minutes"})
METRES = frozenset({"area_total_m2", "area_useful_m2", "area_m2", "terrace_m2"})
YES_NO = frozenset({"furnished", "pets_allowed", "has_elevator", "has_concierge",
                    "gated_community", "has_heating", "has_air_conditioning",
                    "has_pool", "has_gym", "has_terrace", "is_project"})

# A text field's old and new value are not worth quoting in full: a rewritten description
# is a paragraph, and the reader wants to know that it moved, not to diff it in a chat.
LONG = frozenset({"description", "features", "transit", "title", "image_url", "url"})
# Stored as JSON, and the reader wants the minutes: `{"oficina": 32}` is «oficina 32 min».
JOURNEYS = frozenset({"commute"})
DATES = frozenset({"available_from"})

# Both numbers of each verb, because "los estacionamientos subió" is not Spanish and the
# labels are half plural. Which one a field takes is read off its own article rather than
# kept in a second list that would drift out of step with the first.
UP = ("subió", "subieron")
DOWN = ("bajó", "bajaron")
MOVED = ("cambió", "cambiaron")
NOW_SAYS = ("ahora dice", "ahora dicen")
NO_LONGER = ("dejó de estar en el aviso", "dejaron de estar en el aviso")


@dataclass(frozen=True, slots=True)
class Change:
    """One field of one listing that moved, as it will be told."""

    field: str
    old: str | None
    new: str | None
    changed_at: str

    @property
    def label(self) -> str:
        return LABELS.get(self.field, self.field)

    @property
    def plural(self) -> int:
        """1 for a label that names several things, which is what conjugates the verb."""
        return int(self.label.startswith(("Los ", "Las ")))

    def _said(self, verb: tuple[str, str]) -> str:
        return verb[self.plural]

    def _written(self, value: str) -> str:
        """One value the way this field is written: money as money, a flag as sí or no."""
        if self.field in YES_NO:
            return "no" if value in ("0", "0.0", "False") else "sí"
        if self.field in DATES:
            return self._date(value)
        if self.field in JOURNEYS:
            # The unit goes at the end, the way the card writes it: «oficina 32 min».
            travel = commute_text(value)
            return f"{escape(travel)} min" if travel else "—"
        number = _number(value)
        if number is None:
            return escape(value)
        if self.field in MONEY:
            return clp(number)
        if self.field in MINUTES:
            return f"{number:.0f} min"
        if self.field in METRES:
            # A comma for the decimal, and none at all when there is nothing after it.
            return f"{number:g}".replace(".", ",") + " m²"
        return f"{number:g}"

    def _date(self, value: str) -> str:
        """A stored date the way it is written here, and untouched if it is not one."""
        try:
            return datetime.fromisoformat(value).strftime("%d/%m/%Y")
        except ValueError:
            return escape(value)

    @property
    def direction(self) -> tuple[str, str]:
        """Whether it went up, down, or simply moved — only a number can answer that.

        A flag has no direction: an apartment that turned out to be amoblado did not
        have its amoblado "subir", however the 0 and the 1 compare.
        """
        was, now = _number(self.old), _number(self.new)
        if was is None or now is None or self.field in YES_NO:
            return MOVED
        return UP if now > was else DOWN if now < was else MOVED

    def __str__(self) -> str:
        # A rewritten description is a paragraph: that it moved is the news, and the two
        # versions of it are not something to read in a chat.
        if self.field in LONG:
            return f"{self.label} {self._said(MOVED)}"
        # One side missing is a field the portal started or stopped publishing, which
        # reads as nonsense stated as a move from an em dash.
        if self.old is None or self.old == "":
            return f"{self.label} {self._said(NOW_SAYS)} {self._written(self.new or '')}"
        if self.new is None or self.new == "":
            return f"{self.label} {self._said(NO_LONGER)}"
        return (f"{self.label} {self._said(self.direction)} de {self._written(self.old)} "
                f"a {self._written(self.new)}")


def _number(value: str | None) -> float | None:
    """A stored value as a number, or None for one that is not — every value is TEXT here."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def changes_for(connection: sqlite3.Connection, portal: str, external_id: str,
                since: str | None) -> list[Change]:
    """Every reportable move of one listing after `since`, oldest first.

    The price trail and the detail changes are two tables because they are written by
    two different stages — the price comes off the search card every pass, the rest off
    the detail page — and `listing_changes` unions them for reading. It leaves the price's
    old value NULL, though, since a trail of prices has no old column, so the pairing up
    happens here where a rebaja can be stated as one.
    """
    where = "portal = ? AND external_id = ?"
    parameters: list[object] = [portal, external_id]
    changes = [
        Change(row["field"], row["old_value"], row["new_value"], row["changed_at"])
        for row in connection.execute(
            f"SELECT field, old_value, new_value, changed_at FROM detail_changes "
            f"WHERE {where} ORDER BY changed_at", parameters)
        if row["field"] not in NOT_A_CHANGE
    ]
    changes += _price_moves(connection, portal, external_id)
    kept = [one for one in changes if since is None or one.changed_at > since]
    return sorted(kept, key=lambda one: one.changed_at)


def _price_moves(connection: sqlite3.Connection, portal: str,
                 external_id: str) -> list[Change]:
    """The price trail read as moves: each row against the one before it."""
    trail = connection.execute(
        "SELECT price_clp, seen_at FROM price_history WHERE portal = ? AND external_id = ? "
        "ORDER BY seen_at", (portal, external_id),
    ).fetchall()
    # The first row is the price it was first published at, which moved from nothing.
    return [Change("price_clp", str(was["price_clp"]), str(now["price_clp"]), now["seen_at"])
            for was, now in zip(trail, trail[1:], strict=False)
            if was["price_clp"] != now["price_clp"]]


# ── why a card is arriving now ──────────────────────────────────────────────────

# The hard requirements a listing can cross on its own, and the fields that move it
# across. `_requirement_clauses` is what actually gates the announcement; this is which
# of them a change in the listing could have satisfied, so the note can name the reason
# instead of asserting one.
CROSSES = {
    "price": "el costo", "price_clp": "el costo", "common_expenses": "el costo",
    "parking_spaces": "el costo", "storage_units": "el costo",
    "walk_minutes": "la caminata al metro", "commute": "el viaje",
    "area_useful_m2": "el metraje", "area_m2": "el metraje", "area_total_m2": "el metraje",
    "bedrooms": "los dormitorios", "commune": "la comuna",
}

# Under this a listing is simply new, and a note explaining its arrival would be noise.
# Over it, arriving is a fact that wants explaining: the queues are newest-first, so a
# flat can sit unenriched for weeks behind the ones that turned up after it.
LATE_AFTER_HOURS = 24


def why_now(connection: sqlite3.Connection, row: dict[str, Any],
            changes: list[Change]) -> str | None:
    """Why this listing is being announced today rather than when it turned up.

    None for one that simply arrived: a card for a flat seen an hour ago explains itself,
    and a note under every card is a note nobody reads.
    """
    first_seen = row.get("first_seen")
    if not first_seen or first_seen > _hours_ago(LATE_AFTER_HOURS):
        return None
    waited = _waited(first_seen)
    # A vuelta first: it is the only reason that says the listing was out of the pool
    # entirely, which no change to its fields can explain.
    events = listing_events(connection, row["portal"], row["external_id"])
    if events and events[-1]["event"] == "relisted":
        return (f"Volvió a estar publicado en el portal: se había dado de baja "
                f"{_when(events[-1]['happened_at'])} y un barrido lo encontró de nuevo. "
                f"Lo teníamos guardado desde hace {waited}.")
    crossed = [one for one in changes if one.field in CROSSES]
    if crossed:
        what = ", ".join(dict.fromkeys(CROSSES[one.field] for one in crossed))
        return (f"Aparece recién ahora porque cambió {what}, no porque hayas tocado tus "
                f"criterios. Lo teníamos guardado desde hace {waited}.")
    # Nothing about the listing moved, so the wait was ours: the detail page is what the
    # pool requires and its queue is newest-first, so an old row waits behind new ones.
    if row.get("detail_fetched_at") and row["detail_fetched_at"] > _hours_ago(
            LATE_AFTER_HOURS):
        return (f"Aparece recién ahora porque su ficha se leyó hoy: llevaba {waited} "
                f"guardado esperando turno en la cola de enriquecimiento, que lee lo "
                f"más nuevo primero.")
    return (f"Aparece recién ahora aunque lo teníamos guardado desde hace {waited}: "
            f"recién pasó tus requisitos.")


def _hours_ago(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _elapsed(stamp: str) -> timedelta:
    """How long ago a stored timestamp was, tolerating one written without a zone."""
    when = datetime.fromisoformat(stamp)
    return datetime.now(UTC) - (when if when.tzinfo else when.replace(tzinfo=UTC))


def _waited(stamp: str) -> str:
    """How long a listing has been stored, in the coarsest unit that is still true."""
    days = _elapsed(stamp).days
    if days >= 60:
        return f"{days // 30} meses"
    if days >= 2:
        return f"{days} días"
    return "un día" if days == 1 else "unas horas"


def _when(stamp: str) -> str:
    days = _elapsed(stamp).days
    return f"hace {_waited(stamp)}" if days else "hoy"


# ── one listing that moved, as the two messages about it ────────────────────────

# A baja said the way a reader cares about it, keyed by what concluded it. The reason
# matters: a portal answering 404 is the flat being gone, while no sweep finding it is
# an absence counted, and the second is the one that turns out to be wrong sometimes.
BAJA = {GONE_404: "Se dio de baja: el portal ya no publica su ficha",
        GONE_UNSEEN: "Se dio de baja: ningún barrido lo encontró"}
BAJA_UNKNOWN = "Se dio de baja"
VUELTA = "Volvió a estar publicado"
GONE_MARK, BACK_MARK = "⚫ ya no está", "🔄 volvió"

MOST_LINES = 6  # per listing in the digest; the thread comment carries all of them


@dataclass(frozen=True, slots=True)
class Update:
    """One listing a chat already has a card for, and everything that moved since."""

    row: dict[str, Any]
    card: dict[str, Any]
    changes: list[Change]
    events: list[sqlite3.Row]
    grade: Any
    was_letter: str | None
    was_score: int | None

    @property
    def through(self) -> str:
        """The newest thing told here, which is what the watermark moves to."""
        return max([one.changed_at for one in self.changes]
                   + [one["happened_at"] for one in self.events])

    @property
    def gone(self) -> bool:
        return bool(self.events) and self.events[-1]["event"] == "delisted"

    def lines(self) -> list[str]:
        """Everything that moved, one per line, the bajas first because they outrank a price."""
        told = []
        for event in self.events:
            told.append(VUELTA if event["event"] == "relisted"
                        else BAJA.get(event["reason"] or "", BAJA_UNKNOWN))
        return told + [str(one) for one in self.changes]

    def nota(self) -> str | None:
        """How the grade moved, for a card posted by a version that recorded it."""
        if self.was_letter is None or self.was_score is None:
            return None
        if (self.was_letter, self.was_score) == (self.grade.letter, self.grade.score):
            return None
        return (f"La nota pasó de {self.was_letter} {self.was_score} "
                f"a {self.grade.letter} {self.grade.score}.")


def _digest_entry(update: Update, chat_id: str) -> str:
    """One listing in the digest: what it is now, the way back to it, and what moved."""
    row = update.row
    commune = (row.get("commune") or "").replace("-", " ").title()
    head = " · ".join(part for part in (
        GONE_MARK if update.gone else
        f"{GRADE_EMOJI.get(update.grade.letter, '⚪')} <b>{update.grade.letter} "
        f"{update.grade.score}</b>",
        f"era {update.was_letter} {update.was_score}"
        if update.nota() and not update.gone else None,
        escape(commune) or None,
        clp(row.get("net_monthly_clp")),
    ) if part)
    link = message_link(chat_id, update.card["message_id"])
    ways = [f'<a href="{link}">{CARD_LABEL}</a>'] if link else []
    ways.append(f'<a href="{escape(row["url"])}">{LISTING_LABEL}</a>')
    told = update.lines()
    shown = [f"    · {one}" for one in told[:MOST_LINES]]
    if len(told) > MOST_LINES:
        shown.append(f"    · …y {len(told) - MOST_LINES} cambios más")
    return "\n".join([f"{head}\n    {' · '.join(ways)} · <code>[{row['id']}]</code>", *shown])


TITLE = "🔄 <b>Cambió lo que ya te mandé</b>"
THREAD_TITLE = "🔄 <b>Cambió desde que te mandé esta tarjeta</b>"
ARRIVED_TITLE = "🆕 <b>Por qué este aviso aparece recién ahora</b>"
SINCE_FIRST_SEEN = "Lo que cambió desde que lo vimos por primera vez:"


# Telegram rejects a message past its own limit rather than trimming it, so a digest
# that outgrows this is not a long digest but no digest at all.
LIMIT = 4096


def _more(left_out: int) -> str:
    return f"\n…y {left_out} más que cambiaron; salen en la pasada siguiente."


def format_digest(candidates: list[Update], chat_id: str,
                  beyond_budget: int = 0) -> tuple[str, list[Update]]:
    """The one message per pass that names every card that moved, newest movement first.

    Hands back the updates it actually named as well as the message, because those are
    the ones the reader has been told about and so the only ones that may be stamped —
    anything the length budget pushed out has to still be there next pass.
    """
    when = datetime.now(SANTIAGO).strftime("%d/%m %H:%M")
    count = len(candidates) + beyond_budget
    # Everything that moved, not just what fitted: a header saying two above a footer
    # saying two more leaves the reader to add them up.
    header = f"{TITLE} · {count} aviso{'s' if count != 1 else ''} · {when}"

    told: list[Update] = []
    entries: list[str] = []
    # Budgeted against the longest footer it could end up needing, so adding one is
    # never what pushes the whole message past the limit and loses all of it.
    budget = LIMIT - len(header) - len(_more(count))
    for update in candidates:
        entry = _digest_entry(update, chat_id)
        if budget - len(entry) - 2 < 0:
            break
        budget -= len(entry) + 2
        told.append(update)
        entries.append(entry)

    left_out = beyond_budget + len(candidates) - len(told)
    # A blank line between them: each entry is a heading and a list of its own.
    lines = [header, "", "\n\n".join(entries)]
    if left_out:
        lines.append(_more(left_out))
    return "\n".join(lines), told


def format_thread_note(update: Update) -> str:
    """The diff that hangs under the card itself, where whoever is looking at it will be."""
    lines = [THREAD_TITLE, "", *[f"• {one}" for one in update.lines()]]
    nota = update.nota()
    if nota:
        lines += ["", nota]
    return "\n".join(lines)


def format_arrival_note(reason: str, changes: list[Change]) -> str:
    """Why a card for an old listing is arriving now, posted under the card itself."""
    lines = [ARRIVED_TITLE, "", reason]
    if changes:
        lines += ["", SINCE_FIRST_SEEN, *[f"• {one}" for one in changes]]
    return "\n".join(lines)


# ── reading what to tell, and telling it ────────────────────────────────────────

# What a chat has been told about a listing, and everything after it. Cards it never got
# are not in it: a listing stamped without being posted — below the bar — has no message
# to correct and no reader who ever saw it, so a change to it is not news.
CHANGED_SINCE = """
SELECT told.portal, told.external_id, told.grade_letter, told.grade_score,
       COALESCE(mark.through, told.notified_at) AS floor
  FROM subscriber_notifications AS told
  LEFT JOIN update_notifications AS mark
         ON mark.chat_id = told.chat_id AND mark.portal = told.portal
        AND mark.external_id = told.external_id
 WHERE told.chat_id = ?
   AND EXISTS (SELECT 1 FROM card_messages AS card
                WHERE card.chat_id = told.chat_id AND card.portal = told.portal
                  AND card.external_id = told.external_id)
"""

# Cheap enough to ask of every card a chat holds, and it keeps the expensive reading —
# pairing up the price trail, rendering — to the handful that actually moved.
SOMETHING_MOVED = f"""
SELECT * FROM ({CHANGED_SINCE}) AS held
 WHERE EXISTS (SELECT 1 FROM detail_changes AS moved
                WHERE moved.portal = held.portal
                  AND moved.external_id = held.external_id
                  AND moved.changed_at > held.floor)
    OR EXISTS (SELECT 1 FROM price_history AS trail
                WHERE trail.portal = held.portal
                  AND trail.external_id = held.external_id
                  AND trail.seen_at > held.floor)
    OR EXISTS (SELECT 1 FROM delisting_events AS event
                WHERE event.portal = held.portal
                  AND event.external_id = held.external_id
                  AND event.happened_at > held.floor)
"""


def pending(connection: sqlite3.Connection, prefs: Preferences,
            subscriber: Subscriber) -> list[Update]:
    """Every card this chat holds whose listing has moved since it was last told, newest first.

    Read through the subscriber rather than `listings_ranked`, so the grade on the notice
    is the grade the card itself would be redrawn with — a listing somebody in the chat
    discarded reads as discarded here too.
    """
    scale = Scale(prefs)
    updates = []
    for held in connection.execute(SOMETHING_MOVED, (subscriber.chat_id,)).fetchall():
        key = (held["portal"], held["external_id"])
        row = connection.execute(
            f"SELECT * FROM ({subscriber.view()}) WHERE portal = ? AND external_id = ?", key
        ).fetchone()
        card = connection.execute(
            "SELECT * FROM card_messages WHERE chat_id = ? AND portal = ? "
            "AND external_id = ? ORDER BY posted_at DESC LIMIT 1",
            (subscriber.chat_id, *key),
        ).fetchone()
        if row is None or card is None:
            continue  # a card outliving its listing is not a change to report
        changes = changes_for(connection, *key, held["floor"])
        events = listing_events(connection, *key, since=held["floor"])
        if not changes and not events:
            continue  # a price re-recorded at the same figure is not a move
        updates.append(Update(dict(row), dict(card), changes, list(events),
                              scale.grade(dict(row)), held["grade_letter"],
                              held["grade_score"]))
    # Newest movement first: a rebaja from an hour ago is worth more of the budget than
    # one from yesterday that has been sitting there.
    return sorted(updates, key=lambda one: one.through, reverse=True)


def sync(connection: sqlite3.Connection, prefs: Preferences,
         subscriber: Subscriber, limit: int) -> int:
    """Tell one chat what changed about the cards it already has, and correct them.

    Three things per listing, in the order that leaves the chat consistent if it stops
    half way: the card is edited first, so the digest never links to a card still saying
    the old price; the diff goes in its thread, where whoever is looking at the card is;
    and the digest goes last, as the one message that says any of this happened.
    """
    found = pending(connection, prefs, subscriber)
    if not found:
        return 0
    # Written before anything is sent: what the digest could not fit is not corrected
    # either, so a card is never edited without the message that says why it changed.
    digest, told = format_digest(found[:limit], subscriber.chat_id,
                                 max(0, len(found) - limit))
    if not told:
        return 0
    for update in told:
        _correct(connection, prefs, update)
    try:
        reply(subscriber.chat_id, digest)
    except (RuntimeError, ValueError) as error:
        # Nothing is stamped, so the next pass says all of it again rather than never.
        print(f"could not post the digest to {subscriber.chat_id}: {error}")
        return 0
    for update in told:
        mark_updates_reported(connection, subscriber.chat_id, update.row["portal"],
                              update.row["external_id"], update.through, update.grade)
    return len(told)


def _correct(connection: sqlite3.Connection, prefs: Preferences, update: Update) -> None:
    """Edit the card this listing was announced on, and hang the diff under it.

    Total: a card Telegram refuses to edit — too old, deleted by hand — must not cost the
    reader the digest line that says the flat changed.
    """
    try:
        refresh_card(connection, update.card, prefs)
        chat, anchor = card_anchor(update.card)
        reply(chat, format_thread_note(update), reply_to=anchor)
    except (RuntimeError, ValueError) as error:
        print(f"could not correct card {update.card.get('message_id')}: {error}")
