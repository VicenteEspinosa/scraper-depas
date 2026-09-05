import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from curl_cffi import requests

from depas.commute import as_text as commute_text
from depas.config import DEFAULT_COMMON_EXPENSES, secret
from depas.detail import MONTH_NAMES
from depas.grade import BEST as BEST_SCORE
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
        # Telegram puts the actionable part in `parameters`: a migrated chat's new id.
        detail = payload.get("parameters") or ""
        raise RuntimeError(
            f"telegram {method} failed: {payload.get('description')} {detail}".strip())
    return payload["result"]


_CHATS: dict[str, dict[str, Any]] = {}


def _chat(chat_id: str) -> dict[str, Any]:
    """getChat for one chat, asked once per process: what it says only changes on a restart."""
    if chat_id not in _CHATS:
        _CHATS[chat_id] = call("getChat", chat_id=chat_id)
    return _CHATS[chat_id]


def chat_type(chat_id: str) -> str:
    """Whether alerts land in a channel, where each card gets a comment thread, or a group."""
    return _chat(str(chat_id)).get("type", "")


def hides_comments(chat_id: str) -> bool:
    """Whether an inline keyboard posted here would hide the way into a card's comments."""
    # A keyboard takes the slot «Comentarios» lives in: bugs.telegram.org/c/41803.
    chat = _chat(str(chat_id))
    return chat.get("type") == "channel" and chat.get("linked_chat_id") is not None


def _markup(chat_id: str, buttons: dict[str, Any] | None) -> dict[str, Any]:
    """The reply_markup for a card, left off wherever a keyboard would cost the comments."""
    # Asked only when there is a keyboard to place, so a card with none spends no getChat.
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
# Past 100 it has beaten every target across the board, so it gets a banner, not a mark.
FLAWLESS_SCORE = 100
FLAWLESS_BANNER = "💎💎💎💎💎💎💎💎"
TEST_MARK = "🧪"
# The verdict given from the chat, so a scroll through the channel shows what is judged.
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
        cons.append(f"sin conserjería {escape(wanted)}")
    return cons


def _availability(available_from: str) -> str:
    """When the flat frees up; a date already reached is simply entrega inmediata."""
    when = date.fromisoformat(available_from)
    if when <= date.today():
        return "entrega inmediata"
    return f"disponible desde el {when.day} de {MONTH_NAMES[when.month - 1]}"


def clp(amount: float | None) -> str:
    """A CLP figure the way Chile writes it, and an em dash for one nobody stated."""
    return "—" if amount is None else f"${amount:,.0f}".replace(",", ".")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# A move worth reading: under half a percent is rounding, or a portal correcting itself.
PRICE_MOVE_FLOOR = 0.005
DROP_MARK, RISE_MARK = "📉", "📈"


def _days_ago(stamp: str) -> int:
    """Whole days since an ISO stamp, floored at zero for a clock that ran backwards."""
    return max((datetime.now(UTC) - datetime.fromisoformat(stamp)).days, 0)


def _when(days: int) -> str:
    return "hoy" if days == 0 else "ayer" if days == 1 else f"hace {days} días"


def price_change(row: dict[str, Any]) -> tuple[float, float, str] | None:
    """What the asking price did: the share it moved, what it was in CLP, and when.

    A markdown is the strongest thing the portals know and never say — a card states the
    price today and nothing else, so a flat marked down twice reads like one that has
    never budged. `previous_price` is the figure it moved from; the rest is arithmetic."""
    before, price = row.get("previous_price"), row.get("price")
    if not before or not price or abs(price / before - 1) < PRICE_MOVE_FLOOR:
        return None
    # price_clp scales with price at one exchange rate, so today's UF prices both ends:
    # what is compared is the move itself, not a month of UF drift on top of it.
    was = (row.get("price_clp") or 0) * before / price
    return price / before - 1, was, row.get("price_changed_at") or ""


def _price_move(row: dict[str, Any]) -> str | None:
    """One card line for a price that moved, naming the old figure so the move is checkable."""
    change = price_change(row)
    if change is None:
        return None
    share, was, changed_at = change
    mark, verb = (DROP_MARK, "bajó") if share < 0 else (RISE_MARK, "subió")
    moved = abs(was - (row.get("price_clp") or 0))
    when = f" · {_when(_days_ago(changed_at))}" if changed_at else ""
    return f"{mark} <b>{verb} {clp(moved)}</b> ({share:+.0%}){when} · antes {clp(was)}"


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

    lines.append(f"💰 <b>{clp(row.get('net_monthly_clp'))}</b> neto al mes")

    link = f'\n<a href="{escape(row["url"])}">Ver aviso →</a>'
    # A discarded listing keeps only what says which one it was; the decision is made.
    if row.get("interest") == -1:
        lines.append(link)
        return "\n".join(lines)

    gastos = row.get("common_expenses")
    breakdown = f"    ↳ {clp(row.get('price_clp'))} arriendo"
    # The net figure above already includes the estimate, so the card admits which it used.
    lines.append(
        f"{breakdown} + {clp(gastos)} gastos comunes" if gastos
        else f"{breakdown} + {clp(DEFAULT_COMMON_EXPENSES)} gastos comunes "
             "(estimado por defecto, no publicado)"
    )
    sublet = (row.get("parking_spaces") or 0, row.get("storage_units") or 0)
    if any(sublet):
        saved = (row.get("total_monthly_clp") or 0) - (row.get("net_monthly_clp") or 0)
        lines.append(f"    ↳ −{clp(saved)} arrendando {sublet[0]}🚗 {sublet[1]}📦")

    # Right under the price it revises: a markdown is read as part of the figure, not trivia.
    moved = _price_move(row)
    if moved:
        lines.append(moved)

    baseline = prefs.current_cost()
    net = row.get("net_monthly_clp")
    if baseline and net is not None:
        difference = net - baseline
        if difference == 0:
            lines.append("⚖️ lo mismo que pagas hoy")
        else:
            mark, word = ("🔺", "más caro") if difference > 0 else ("🔻", "más barato")
            lines.append(f"⚖️ {mark} {clp(abs(difference))} {word} que hoy")

    station = row.get("nearest_station")
    if station:
        lines.append(f"🚇 {_station(station)} · {row.get('walk_minutes')} min caminando")

    travel = commute_text(row.get("commute"))
    if travel:
        lines.append(f"🧭 {escape(travel)} min")

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


# What each graded component measures, in the vocabulary the settings menu already uses.
COMPONENT_LABELS = {
    "value": "precio zona", "cost": "costo", "walk": "caminata", "area": "metraje",
    "amenities": "comodidades", "security": "conserjería", "floor": "piso",
    "metro": "metro", "commute": "viajes", "age": "antigüedad",
    "availability": "entrega", "traits": "características",
}
# Why a component went unscored, so an absent row reads as silence rather than a zero.
UNSCORED = "el aviso no lo dice, o no lo has configurado"
BAR_CELLS = 10
FULL_CELL, EMPTY_CELL = "█", "·"
WEAKEST_MARK = "← lo más flojo"


def _bar(score: int) -> str:
    """One component's score as a fixed-width bar, so a column of them is scannable."""
    filled = max(0, min(BAR_CELLS, round(score / (BEST_SCORE / BAR_CELLS))))
    return FULL_CELL * filled + EMPTY_CELL * (BAR_CELLS - filled)


def format_breakdown(grade: Any, prefs: Preferences) -> str:
    """Render the twelve components behind a grade, worst last, as the card's own audit."""
    weights = prefs.weights()
    scored = sorted(grade.parts.items(), key=lambda part: part[1], reverse=True)
    width = max((len(COMPONENT_LABELS[name]) for name, _ in scored), default=0)

    rows = []
    for index, (name, score) in enumerate(scored):
        weight = weights.get(name, 1)
        # A weight of 1 is the default and says nothing; anything else explains the grade.
        heavier = f" ×{weight:g}" if weight != 1 else ""
        # Only worth pointing at when there is something above it to be flojo against.
        weakest = f"  {WEAKEST_MARK}" if index and index == len(scored) - 1 else ""
        label = COMPONENT_LABELS[name].ljust(width)
        rows.append(f"{label}  {_bar(score)} {score:>3}{heavier}{weakest}")

    total = len(grade.parts) + len(grade.missing)
    table = escape("\n".join(rows))
    lines = [f"📊 <b>{grade.letter} {grade.score}</b> · "
             f"{len(grade.parts)} de {total} componentes",
             f"<pre>{table}</pre>"]
    if grade.missing:
        absent = " · ".join(COMPONENT_LABELS[name] for name in grade.missing)
        lines.append(f"❓ sin puntaje: {escape(absent)}\n<i>{UNSCORED}</i>")
    if grade.meets_targets:
        lines.append(f"{MEETS_TARGETS_MARK} cumple todos los objetivos que pudo medir")
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
    ("💰 neto al mes", "net_monthly_clp", clp, True),
    ("🏷️ arriendo", "price_clp", clp, True),
    ("🧾 gastos comunes", "common_expenses", clp, True),
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
    return [_difference(f"🧭 {escape(name)}", here[name], minutes, _minutes, True)
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
    # callback_data is capped at 64 bytes, so what travels is the listing's rowid.
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
    """Post the verdict keyboard as a comment inside a card's thread."""
    # A group is no forum, so what puts this under the card is replying to its copy.
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
    """Post the card, as a photo when the listing has one and the caption fits."""
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
    # An edit that omits reply_markup drops the keyboard, so the buttons ride every edit.
    markup = _markup(chat_id, buttons)
    # A photo card holds its text in the caption, a different method and a different field.
    if is_photo:
        call("editMessageCaption", chat_id=chat_id, message_id=message_id,
             caption=text, parse_mode="HTML", **markup)
        return
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
         parse_mode="HTML", link_preview_options={"is_disabled": True}, **markup)


# The config menu is only ever answered to a person, so these three skip `_markup`.


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


def message_link(chat_id: object, message_id: int) -> str | None:
    """A deep link straight to one message, which only a channel or supergroup can give."""
    # Their ids are the internal one behind a -100 prefix, and t.me/c wants it back off.
    internal = str(chat_id).removeprefix("-100")
    if internal == str(chat_id) or not internal.isdigit():
        return None  # a private chat or a plain group: nothing to link to
    return f"https://t.me/c/{internal}/{message_id}"


def pin(chat_id: str, message_id: int) -> None:
    """Pin one message, silently: a list that re-pins itself must not notify every time."""
    call("pinChatMessage", chat_id=chat_id, message_id=message_id,
         disable_notification=True)


def edit_text(chat_id: str, message_id: int, text: str) -> None:
    """Re-render a plain message in place: one that never carried a keyboard to preserve."""
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
         parse_mode="HTML", link_preview_options={"is_disabled": True})


def ask_value(chat_id: str, text: str, thread_id: int | None = None) -> dict[str, Any]:
    """Ask for a value no keyboard can offer, as a reply the answer will quote back."""
    # Not `selective`: posted from a button press, it has nobody to target, so nobody replies.
    where: dict[str, Any] = {"message_thread_id": thread_id} if thread_id else {}
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                reply_markup={"force_reply": True},
                link_preview_options={"is_disabled": True}, **where)


def reply(chat_id: str, text: str, thread_id: int | None = None,
          reply_to: int | None = None) -> dict[str, Any]:
    """Answer one message: in its thread, and quoting the message that asked."""
    where: dict[str, Any] = {}
    if thread_id:
        where["message_thread_id"] = thread_id
    if reply_to:
        # allow_sending_without_reply: the answer still matters if the command was deleted.
        where["reply_parameters"] = {"message_id": reply_to,
                                     "allow_sending_without_reply": True}
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", **where)
