import os
from typing import Any

from curl_cffi import requests

from depas.commute import as_text as commute_text
from depas.config import (DEFAULT_COMMON_EXPENSES, _load_env_file, current_cost,
                          optional_int, optional_text, target_age)
from depas.metro import STATION_LINES

API = "https://api.telegram.org"
TIMEOUT = 40


def bot_token() -> str:
    _load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
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


def chat_type(chat_id: str) -> str:
    """Whether alerts land in a channel, where each card gets a comment thread, or a group."""
    return call("getChat", chat_id=chat_id)["type"]


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


def _cons(row: dict[str, Any]) -> list[str]:
    """The preferences this listing misses, so a docked score is legible in the card."""
    cons = []
    floor, top = row.get("floor"), row.get("building_floors")
    target = optional_int("DEPAS_TARGET_FLOOR")
    if floor is not None and target is not None and floor < target:
        cons.append(f"piso {floor}, bajo el {target}º")
    if floor is not None and floor == top:
        cons.append(f"último piso ({floor} de {top})")
    area, target_area = row.get("area"), optional_int("DEPAS_TARGET_AREA")
    if area is None:
        cons.append("metraje no publicado")
    elif target_area is not None and area < target_area:
        cons.append(f"{area:.0f} m², bajo los {target_area}")
    age, oldest = row.get("age"), target_age()
    if age is not None and age > oldest:
        cons.append(f"{age:.0f} años, sobre los {oldest}")
    wanted = optional_text("DEPAS_ALERT_SECURITY")
    if wanted and row.get("security_type") != wanted:
        cons.append(f"sin conserjería {wanted}")
    return cons


def _clp(amount: float | None) -> str:
    return "—" if amount is None else f"${amount:,.0f}".replace(",", ".")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_listing(row: dict[str, Any], grade: Any, is_test: bool = False) -> str:
    """Render one listing as the Telegram HTML card posted to the chat."""
    emoji = GRADE_EMOJI.get(grade.letter, "⚪")
    data_mark = PARTIAL_MARK if grade.missing else COMPLETE_MARK
    prefix = "".join(f"{mark} " for mark in (TEST_MARK if is_test else None,
                                             INTEREST_MARK.get(row.get("interest"))) if mark)
    commune = (row.get("commune") or "").replace("-", " ").title()
    header = [f"{prefix}{emoji} <b>{grade.letter} {grade.score}</b> {data_mark}"]
    if commune:  # a listing whose portal never stated one would leave a dangling separator
        header.append(f"<b>{_escape(commune)}</b>")
    if row.get("id"):
        header.append(f"<code>[{row['id']}]</code>")
    lines = [" · ".join(header)]
    if row.get("title"):
        lines.append(f"<i>{_escape(row['title'])}</i>")

    spec = [f"{row['bedrooms']}D" if row.get("bedrooms") else None,
            f"{row['bathrooms']}B" if row.get("bathrooms") else None,
            f"{row['area']:.0f} m²" if row.get("area") else None,
            f"piso {row['floor']}" if row.get("floor") else None,
            # `is not None`: a brand-new building is 0 años, which is worth printing.
            f"{row['age']:.0f} años" if row.get("age") is not None else None]
    lines.append("🏠 " + " · ".join(part for part in spec if part))

    lines.append(f"💰 <b>{_clp(row.get('net_monthly_clp'))}</b> neto al mes")
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

    baseline = current_cost()
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
        calling = STATION_LINES.get(station, ())
        label = f" (L{'/L'.join(calling)})" if calling else ""
        lines.append(f"🚇 {_escape(station)}{label} · {row.get('walk_minutes')} min caminando")

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

    cons = _cons(row)
    if cons:
        lines.append("👎 " + " · ".join(cons))

    if row.get("published_days_ago") is not None:
        lines.append(f"🕐 publicado hace {row['published_days_ago']} días")

    lines.append(f'\n<a href="{_escape(row["url"])}">Ver aviso →</a>')
    return "\n".join(lines)


CAPTION_LIMIT = 1024


def send_listing(chat_id: str, text: str, image_url: str | None = None,
                 thread_id: int | None = None) -> dict[str, Any]:
    """Post the card, as a photo when the listing has one and the caption fits.

    Returns Telegram's own record of the message: its ids are what a later edit is
    addressed to, and what a command commented under the card is traced back through.
    """
    # Only ever sent when replying inside a comment thread; Telegram rejects a null.
    thread = {"message_thread_id": thread_id} if thread_id else {}
    if image_url and len(text) <= CAPTION_LIMIT:
        try:
            return call("sendPhoto", chat_id=chat_id, photo=image_url, caption=text,
                        parse_mode="HTML", **thread)
        except RuntimeError:
            # Telegram rejects some remote images (size, host, format); the card still matters.
            pass
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                link_preview_options={"is_disabled": True}, **thread)


def edit_listing(chat_id: str, message_id: int, text: str, is_photo: bool = False) -> None:
    """Re-render a card already posted, in place."""
    # A photo card holds its text in the caption, which is a different edit method
    # and a different field; only a text card has a link preview to suppress.
    if is_photo:
        call("editMessageCaption", chat_id=chat_id, message_id=message_id,
             caption=text, parse_mode="HTML")
        return
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
         parse_mode="HTML", link_preview_options={"is_disabled": True})


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
