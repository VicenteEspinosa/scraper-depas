import os
from typing import Any

from curl_cffi import requests

from depas.config import _load_env_file, current_cost, optional_int, optional_text

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
        raise RuntimeError(f"telegram {method} failed: {payload.get('description')}")
    return payload["result"]


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
    wanted = optional_text("DEPAS_ALERT_SECURITY")
    if wanted and row.get("security_type") != wanted:
        cons.append(f"sin conserjería {wanted}")
    return cons


def _clp(amount: float | None) -> str:
    return "—" if amount is None else f"${amount:,.0f}".replace(",", ".")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_listing(row: dict[str, Any], grade: Any, is_test: bool = False) -> str:
    """Render one listing as the Telegram HTML card posted to the group."""
    emoji = GRADE_EMOJI.get(grade.letter, "⚪")
    data_mark = PARTIAL_MARK if grade.missing else COMPLETE_MARK
    prefix = f"{TEST_MARK} " if is_test else ""
    commune = (row.get("commune") or "").replace("-", " ").title()
    lines = [f"{prefix}{emoji} <b>{grade.letter} {grade.score}</b> {data_mark} "
             f"· <b>{_escape(commune)}</b>"]
    if row.get("title"):
        lines.append(f"<i>{_escape(row['title'])}</i>")

    spec = [f"{row['bedrooms']}D" if row.get("bedrooms") else None,
            f"{row['bathrooms']}B" if row.get("bathrooms") else None,
            f"{row['area']:.0f} m²" if row.get("area") else None,
            f"piso {row['floor']}" if row.get("floor") else None]
    lines.append("🏠 " + " · ".join(part for part in spec if part))

    lines.append(f"💰 <b>{_clp(row.get('net_monthly_clp'))}</b> neto al mes")
    gastos = row.get("common_expenses")
    breakdown = f"    ↳ {_clp(row.get('price_clp'))} arriendo"
    lines.append(
        f"{breakdown} + {_clp(gastos)} gastos comunes" if gastos
        else f"{breakdown} + gastos comunes"
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

    if row.get("nearest_station"):
        lines.append(f"🚇 {_escape(row['nearest_station'])} · {row.get('walk_minutes')} min caminando")

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


def send_listing(chat_id: str, text: str, image_url: str | None = None) -> None:
    """Post the card, as a photo when the listing has one and the caption fits."""
    if image_url and len(text) <= CAPTION_LIMIT:
        try:
            call("sendPhoto", chat_id=chat_id, photo=image_url, caption=text, parse_mode="HTML")
            return
        except RuntimeError:
            # Telegram rejects some remote images (size, host, format); the card still matters.
            pass
    call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
         link_preview_options={"is_disabled": True})
