import json
from collections.abc import Callable
from datetime import date
from typing import Any

from curl_cffi import requests

from depas.commute import as_text as commute_text
from depas.config import DEFAULT_COMMON_EXPENSES, secret
from depas.detail import MONTH_NAMES
from depas.metro import STATION_LINES
from depas.preferences import Preferences

API = "https://api.telegram.org"
TIMEOUT = 40


def bot_token() -> str:
    """The bot's credential, which stays in the environment rather than the database."""
    token = secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("set TELEGRAM_BOT_TOKEN in .env (get one from @BotFather)")
    return token


def call(method: str, **params: Any) -> Any:
    """Invoke one Bot API method, raising with Telegram's own message on failure."""
    response = requests.post(f"{API}/bot{bot_token()}/{method}", json=params, timeout=TIMEOUT)
    payload = response.json()
    if not payload.get("ok"):
        # Telegram puts the actionable part in `parameters` — a migrated chat's new id
        # lands there, and dropping it turns a one-line fix into a debugging session.
        detail = payload.get("parameters") or ""
        raise RuntimeError(f"telegram {method} failed: {payload.get('description')} {detail}".strip())
    return payload["result"]


_CHATS: dict[str, dict[str, Any]] = {}


def _chat(chat_id: str) -> dict[str, Any]:
    """getChat for one chat, asked once per process rather than once per card.

    What is read out of it — the type, and whether a discussion group is linked —
    only changes when the channel itself is reconfigured, which is a restart.
    """
    if chat_id not in _CHATS:
        _CHATS[chat_id] = call("getChat", chat_id=chat_id)
    return _CHATS[chat_id]


def chat_type(chat_id: str) -> str:
    """Whether alerts land in a channel, where each card gets a comment thread, or a group."""
    return _chat(str(chat_id)).get("type", "")


def hides_comments(chat_id: str) -> bool:
    """Whether an inline keyboard posted here would hide the way into a card's comments.

    A channel post's «Comentarios» button and a bot's inline keyboard share the one
    slot under the message, and the keyboard wins: attach one to a card in a channel
    with a linked discussion group and the thread can no longer be opened from the
    channel at all (bugs.telegram.org/c/41803). So a card posted there carries no
    keyboard, and the verdict buttons are posted into the thread instead.
    """
    chat = _chat(str(chat_id))
    return chat.get("type") == "channel" and chat.get("linked_chat_id") is not None


def _markup(chat_id: str, buttons: dict[str, Any] | None) -> dict[str, Any]:
    """The reply_markup for a card, left off wherever a keyboard would cost the comments."""
    # Asked only when there is a keyboard to place, so a card with no buttons never
    # spends a getChat call.
    if not buttons or hides_comments(chat_id):
        return {}
    return {"reply_markup": buttons}


def chats() -> list[dict[str, Any]]:
    """Every chat the bot has seen a message from, newest update wins."""
    seen: dict[int, dict[str, Any]] = {}
    for update in call("getUpdates"):
        message = update.get("message") or update.get("channel_post")
        if message:
            chat = message["chat"]
            seen[chat["id"]] = chat
    return list(seen.values())


GRADE_EMOJI = {"A": "🟢", "B": "🟢", "C": "🟡", "D": "🟠", "E": "🔴", "?": "⚪"}
# How much of the grade is real: every component scored, or some of them absent.
COMPLETE_MARK = "✔️"
PARTIAL_MARK = "❓"
# At or past every target it could be scored on: nothing was compromised.
MEETS_TARGETS_MARK = "✅"
# Past 100 the listing has not merely met the targets, it has beaten them across the
# board — the flat this whole thing is looking for. It gets a banner, not a mark.
FLAWLESS_SCORE = 100
FLAWLESS_BANNER = "💎💎💎💎💎💎💎💎"
TEST_MARK = "🧪"
# The verdict given from the chat, so a scroll through the channel shows at a
# glance what has already been judged.
INTEREST_MARK = {1: "⭐", -1: "🚫"}
AMENITY_LABELS = (
    ("has_elevator", "ascensor"), ("has_concierge", "conserjería"),
    ("has_pool", "piscina"), ("has_gym", "gimnasio"), ("has_heating", "calefacción"),
    ("has_air_conditioning", "aire acond."), ("gated_community", "condominio"),
    ("pets_allowed", "mascotas"), ("has_terrace", "terraza"),
)


def _cons(row: dict[str, Any], prefs: Preferences) -> list[str]:
    """The preferences this listing misses, so a docked score is legible in the card."""
    cons = []
    floor, top = row.get("floor"), row.get("building_floors")
    target = prefs.floor.target
    if floor is not None and target is not None and floor < target:
        cons.append(f"piso {floor}, bajo el {target}º")
    if floor is not None and floor == top:
        cons.append(f"último piso ({floor} de {top})")
    area, target_area = row.get("area"), prefs.area.target
    if area is None:
        cons.append("metraje no publicado")
    elif target_area is not None and area < target_area:
        cons.append(f"{area:.0f} m², bajo los {target_area}")
    age, oldest = row.get("age"), prefs.age.target
    if age is not None and age > oldest:
        cons.append(f"{age:.0f} años, sobre los {oldest}")
    wanted = prefs.security_wanted()
    if wanted and row.get("security_type") != wanted:
        cons.append(f"sin conserjería {wanted}")
    return cons


def _availability(available_from: str) -> str:
    """When the flat frees up; a date already reached is simply entrega inmediata."""
    when = date.fromisoformat(available_from)
    if when <= date.today():
        return "entrega inmediata"
    return f"disponible desde el {when.day} de {MONTH_NAMES[when.month - 1]}"


def _clp(amount: float | None) -> str:
    return "—" if amount is None else f"${amount:,.0f}".replace(",", ".")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _station(name: str) -> str:
    """A station with the lines calling at it, blank when the portal named one we do not know."""
    calling = STATION_LINES.get(name, ())
    return f"{escape(name)} (L{'/L'.join(calling)})" if calling else escape(name)


def format_listing(row: dict[str, Any], grade: Any, prefs: Preferences,
                   is_test: bool = False) -> str:
    """Render one listing as the Telegram HTML card posted to the chat."""
    emoji = GRADE_EMOJI.get(grade.letter, "⚪")
    marks = [PARTIAL_MARK if grade.missing else COMPLETE_MARK,
             MEETS_TARGETS_MARK if grade.meets_targets else None]
    prefix = "".join(f"{mark} " for mark in (TEST_MARK if is_test else None,
                                             INTEREST_MARK.get(row.get("interest"))) if mark)
    commune = (row.get("commune") or "").replace("-", " ").title()
    header = [f"{prefix}{emoji} <b>{grade.letter} {grade.score}</b> "
              + " ".join(mark for mark in marks if mark)]
    if commune:  # a listing whose portal never stated one would leave a dangling separator
        header.append(f"<b>{escape(commune)}</b>")
    if row.get("id"):
        header.append(f"<code>[{row['id']}]</code>")
    lines = [FLAWLESS_BANNER] if grade.score >= FLAWLESS_SCORE else []
    lines.append(" · ".join(header))
    if row.get("title"):
        lines.append(f"<i>{escape(row['title'])}</i>")

    spec = [f"{row['bedrooms']}D" if row.get("bedrooms") else None,
            f"{row['bathrooms']}B" if row.get("bathrooms") else None,
            f"{row['area']:.0f} m²" if row.get("area") else None,
            f"piso {row['floor']}" if row.get("floor") else None,
            # `is not None`: a brand-new building is 0 años, which is worth printing.
            f"{row['age']:.0f} años" if row.get("age") is not None else None]
    lines.append("🏠 " + " · ".join(part for part in spec if part))

    lines.append(f"💰 <b>{_clp(row.get('net_monthly_clp'))}</b> neto al mes")

    link = f'\n<a href="{escape(row["url"])}">Ver aviso →</a>'
    # A discarded listing keeps only what says which one it was; everything below is
    # there to decide with, and that decision has been made.
    if row.get("interest") == -1:
        lines.append(link)
        return "\n".join(lines)

    gastos = row.get("common_expenses")
    breakdown = f"    ↳ {_clp(row.get('price_clp'))} arriendo"
    # Undeclared gastos comunes are estimated, and the net figure above already
    # includes the estimate, so the card has to admit which number it used.
    lines.append(
        f"{breakdown} + {_clp(gastos)} gastos comunes" if gastos
        else f"{breakdown} + {_clp(DEFAULT_COMMON_EXPENSES)} gastos comunes "
             "(estimado por defecto, no publicado)"
    )
    sublet = (row.get("parking_spaces") or 0, row.get("storage_units") or 0)
    if any(sublet):
        saved = (row.get("total_monthly_clp") or 0) - (row.get("net_monthly_clp") or 0)
        lines.append(f"    ↳ −{_clp(saved)} arrendando {sublet[0]}🚗 {sublet[1]}📦")

    baseline = prefs.current_cost()
    net = row.get("net_monthly_clp")
    if baseline and net is not None:
        difference = net - baseline
        if difference == 0:
            lines.append("⚖️ lo mismo que pagas hoy")
        else:
            mark, word = ("🔺", "más caro") if difference > 0 else ("🔻", "más barato")
            lines.append(f"⚖️ {mark} {_clp(abs(difference))} {word} que hoy")

    station = row.get("nearest_station")
    if station:
        lines.append(f"🚇 {_station(station)} · {row.get('walk_minutes')} min caminando")

    travel = commute_text(row.get("commute"))
    if travel:
        lines.append(f"🧭 {travel} min")

    asking = row.get("price_per_m2_uf_effective")
    zone = row.get("zone_price_per_m2_uf_effective")
    if asking and zone:
        delta = (asking / zone - 1) * 100
        mark = "🔻" if delta < 0 else "🔺"
        lines.append(f"📊 {asking:.2f} UF/m² vs {zone:.2f} zona {mark} {abs(delta):.0f}%")

    amenities = [label for column, label in AMENITY_LABELS if row.get(column)]
    if amenities:
        lines.append("✨ " + " · ".join(amenities[:5]))

    cons = _cons(row, prefs)
    if cons:
        lines.append("👎 " + " · ".join(cons))

    if row.get("available_from"):
        lines.append(f"🗓️ {_availability(row['available_from'])}")

    if row.get("published_days_ago") is not None:
        lines.append(f"🕐 publicado hace {row['published_days_ago']} días")

    lines.append(link)
    return "\n".join(lines)


def _m2(value: float) -> str:
    return f"{value:.0f} m²"


def _years(value: float) -> str:
    return f"{value:.0f} años"


def _uf_m2(value: float) -> str:
    return f"{value:.2f} UF/m²"


def _minutes(value: float) -> str:
    return f"{value:.0f} min"


def _count(value: float) -> str:
    return f"{value:.0f}"


# Every figure both a listing and your own place carry, and which way is better.
COMPARED = (
    ("💰 neto al mes", "net_monthly_clp", _clp, True),
    ("🏷️ arriendo", "price_clp", _clp, True),
    ("🧾 gastos comunes", "common_expenses", _clp, True),
    ("📐 superficie", "area", _m2, False),
    ("🛏️ dormitorios", "bedrooms", _count, False),
    ("🚿 baños", "bathrooms", _count, False),
    ("🏢 piso", "floor", _count, False),
    ("🏗️ antigüedad", "age", _years, True),
    ("📊 precio por m²", "price_per_m2_uf_effective", _uf_m2, True),
    ("🚶 caminata al metro", "walk_minutes", _minutes, True),
)


def _difference(label: str, before: float, after: float,
                render: Callable[[float], str], lower_is_better: bool) -> str:
    """One figure as `tuyo → este`, with the gap named mejor or peor for that figure."""
    if before == after:
        return f"{label}: {render(after)} · igual"
    mark = "🔻" if after < before else "🔺"
    verdict = "mejor" if (after < before) == lower_is_better else "peor"
    return (f"{label}: {render(before)} → {render(after)} · "
            f"{mark} {render(abs(after - before))} {verdict}")


def _commute_lines(home: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """One line per configured location, so a move is judged on every trip it changes."""
    here = json.loads(home.get("commute") or "{}")
    there = json.loads(row.get("commute") or "{}")
    return [_difference(f"🧭 {name}", here[name], minutes, _minutes, True)
            for name, minutes in there.items() if name in here]


def _amenity_lines(home: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """What the move would add and what it would cost, so a swap is not read as a gain."""
    gained = [label for column, label in AMENITY_LABELS
              if row.get(column) and not home.get(column)]
    lost = [label for column, label in AMENITY_LABELS
            if home.get(column) and not row.get(column)]
    lines = []
    if gained:
        lines.append(f"✨ gana: {' · '.join(gained)}")
    if lost:
        lines.append(f"👎 pierde: {' · '.join(lost)}")
    return lines


def format_comparison(row: dict[str, Any], grade: Any,
                      home: dict[str, Any], home_grade: Any) -> str:
    """Render one listing against the place you live in now, as `tuyo → este` per figure."""
    commune = (row.get("commune") or "").replace("-", " ").title()
    lines = [f"⚖️ <b>Tu depto → este aviso</b> · <code>[{row['id']}]</code>",
             f"{GRADE_EMOJI.get(home_grade.letter, '⚪')} {home_grade.letter} {home_grade.score}"
             f" → {GRADE_EMOJI.get(grade.letter, '⚪')} <b>{grade.letter} {grade.score}</b>"]
    if commune:
        home_commune = (home.get("commune") or "").replace("-", " ").title()
        lines.append(f"📍 {escape(home_commune) or '—'} → <b>{escape(commune)}</b>")

    for label, column, render, lower_is_better in COMPARED:
        before, after = home.get(column), row.get(column)
        if before is not None and after is not None:
            lines.append(_difference(label, before, after, render, lower_is_better))

    station, home_station = row.get("nearest_station"), home.get("nearest_station")
    if station and home_station:
        lines.append(f"🚇 {_station(home_station)} → {_station(station)}")

    lines += _commute_lines(home, row)
    lines += _amenity_lines(home, row)
    lines.append(f'\n<a href="{escape(row["url"])}">Ver aviso →</a>')
    return "\n".join(lines)


CAPTION_LIMIT = 1024


LIKE_BUTTON, DISLIKE_BUTTON, UNDO_BUTTON = "like", "dislike", "undo"
UNDO_LABEL = {1: "⭐ Interesa · ↩️ deshacer", -1: "🚫 Descartado · ↩️ deshacer"}


def verdict_buttons(listing_id: int, interest: int | None = None) -> dict[str, Any]:
    """The keyboard under a card: both verdicts, or the way back from the one given."""
    # callback_data is capped at 64 bytes, so what travels is the listing's rowid --
    # stable for the life of a listing -- rather than the portal and its external id.
    if interest is not None:
        return {"inline_keyboard": [[
            {"text": UNDO_LABEL[interest], "callback_data": f"{UNDO_BUTTON}:{listing_id}"},
        ]]}
    return {"inline_keyboard": [[
        {"text": "⭐ Me interesa", "callback_data": f"{LIKE_BUTTON}:{listing_id}"},
        {"text": "🚫 Descartar", "callback_data": f"{DISLIKE_BUTTON}:{listing_id}"},
    ]]}


def answer_callback(callback_id: str, text: str) -> None:
    """Stop the button's spinner, saying what happened as a toast rather than a message."""
    call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def send_buttons(chat_id: str, text: str, thread_id: int,
                 buttons: dict[str, Any]) -> dict[str, Any]:
    """Post the verdict keyboard as a comment inside a card's thread.

    Where the card itself cannot hold the keyboard — see `hides_comments` — this is
    where it goes: one level down, in the discussion group, which is a plain group
    as far as reply markup is concerned.
    """
    # A discussion group is not a forum, so message_thread_id alone leaves the message
    # loose in the group: what puts it under the card is replying to Telegram's copy of
    # it, whose id is the thread's.
    return call("sendMessage", chat_id=chat_id, text=text,
                reply_parameters={"message_id": thread_id},
                reply_markup=buttons, link_preview_options={"is_disabled": True})


def edit_buttons(chat_id: str, message_id: int, buttons: dict[str, Any]) -> None:
    """Re-tick a keyboard in place, leaving the text it hangs off alone."""
    call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
         reply_markup=buttons)


def send_listing(chat_id: str, text: str, image_url: str | None = None,
                 thread_id: int | None = None,
                 buttons: dict[str, Any] | None = None) -> dict[str, Any]:
    """Post the card, as a photo when the listing has one and the caption fits.

    Returns Telegram's own record of the message: its ids are what a later edit is
    addressed to, and what a command commented under the card is traced back through.
    """
    # Only ever sent when replying inside a comment thread; Telegram rejects a null.
    thread = {"message_thread_id": thread_id} if thread_id else {}
    thread |= _markup(chat_id, buttons)
    if image_url and len(text) <= CAPTION_LIMIT:
        try:
            return call("sendPhoto", chat_id=chat_id, photo=image_url, caption=text,
                        parse_mode="HTML", **thread)
        except RuntimeError:
            # Telegram rejects some remote images (size, host, format); the card still matters.
            pass
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                link_preview_options={"is_disabled": True}, **thread)


def edit_listing(chat_id: str, message_id: int, text: str, is_photo: bool = False,
                 buttons: dict[str, Any] | None = None) -> None:
    """Re-render a card already posted, in place."""
    # An edit that omits reply_markup drops the keyboard, so the buttons have to be
    # sent again every time — there is no such thing as editing only the text. That
    # also means redrawing a channel card, where `_markup` withholds them, is what
    # gives a card posted with a keyboard its «Comentarios» button back.
    markup = _markup(chat_id, buttons)
    # A photo card holds its text in the caption, which is a different edit method
    # and a different field; only a text card has a link preview to suppress.
    if is_photo:
        call("editMessageCaption", chat_id=chat_id, message_id=message_id,
             caption=text, parse_mode="HTML", **markup)
        return
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
         parse_mode="HTML", link_preview_options={"is_disabled": True}, **markup)


# The config menu never goes to a channel -- it is only ever answered to a person the
# whitelist recognises, and a channel post has no person to recognise -- so these three
# attach their keyboard directly rather than through `_markup`, which exists to protect
# a channel card's «Comentarios» button.


def send_menu(chat_id: str, text: str, buttons: dict[str, Any] | None,
              thread_id: int | None = None, reply_to: int | None = None) -> dict[str, Any]:
    """Post one screen of the settings menu, or a plain answer where there is no keyboard."""
    where: dict[str, Any] = {"reply_markup": buttons} if buttons else {}
    if thread_id:
        where["message_thread_id"] = thread_id
    if reply_to:
        where["reply_parameters"] = {"message_id": reply_to,
                                     "allow_sending_without_reply": True}
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                link_preview_options={"is_disabled": True}, **where)


def edit_menu(chat_id: str, message_id: int, text: str, buttons: dict[str, Any]) -> None:
    """Move the menu to another screen in place, rather than posting one message a press."""
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
         parse_mode="HTML", link_preview_options={"is_disabled": True},
         reply_markup=buttons)


def ask_value(chat_id: str, text: str, thread_id: int | None = None) -> dict[str, Any]:
    """Ask for a value no keyboard can offer, as a reply the answer will quote back."""
    # force_reply is what makes the answer carry reply_to_message, which is where the
    # setting being edited is read back from -- there is no pending-edit state anywhere.
    where: dict[str, Any] = {"message_thread_id": thread_id} if thread_id else {}
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                reply_markup={"force_reply": True, "selective": True},
                link_preview_options={"is_disabled": True}, **where)


def reply(chat_id: str, text: str, thread_id: int | None = None,
          reply_to: int | None = None) -> dict[str, Any]:
    """Answer one message: in its thread, and quoting the message that asked."""
    where: dict[str, Any] = {}
    if thread_id:
        where["message_thread_id"] = thread_id
    if reply_to:
        # allow_sending_without_reply: the answer still matters if the command was
        # deleted between arriving and being handled.
        where["reply_parameters"] = {"message_id": reply_to,
                                     "allow_sending_without_reply": True}
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", **where)
