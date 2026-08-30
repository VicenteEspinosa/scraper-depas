import os
from typing import Any

from curl_cffi import requests

from depas.config import _load_env_file

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
AMENITY_LABELS = (
    ("has_elevator", "ascensor"), ("has_concierge", "conserjería"),
    ("has_pool", "piscina"), ("has_gym", "gimnasio"), ("has_heating", "calefacción"),
    ("has_air_conditioning", "aire acond."), ("gated_community", "condominio"),
    ("pets_allowed", "mascotas"), ("has_terrace", "terraza"),
)


def _clp(amount: float | None) -> str:
    return "—" if amount is None else f"${amount:,.0f}".replace(",", ".")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_listing(row: dict[str, Any], grade: Any) -> str:
    """Render one listing as the Telegram HTML card posted to the group."""
    emoji = GRADE_EMOJI.get(grade.letter, "⚪")
    partial = " ⚠️" if grade.missing else ""
    commune = (row.get("commune") or "").replace("-", " ").title()
    lines = [f"{emoji} <b>{grade.letter} {grade.score}</b>{partial} · <b>{_escape(commune)}</b>"]

    spec = [f"{row['bedrooms']}D" if row.get("bedrooms") else None,
            f"{row['bathrooms']}B" if row.get("bathrooms") else None,
            f"{row['area']:.0f} m²" if row.get("area") else None,
            f"piso {row['floor']}" if row.get("floor") else None]
    lines.append("🏠 " + " · ".join(part for part in spec if part))

    lines.append(f"💰 <b>{_clp(row.get('net_monthly_clp'))}</b> neto al mes")
    lines.append(f"    ↳ {_clp(row.get('price_clp'))} arriendo + {_clp(row.get('common_expenses'))} gastos")
    sublet = (row.get("parking_spaces") or 0, row.get("storage_units") or 0)
    if any(sublet):
        saved = (row.get("total_monthly_clp") or 0) - (row.get("net_monthly_clp") or 0)
        lines.append(f"    ↳ −{_clp(saved)} arrendando {sublet[0]}🚗 {sublet[1]}📦")

    if row.get("nearest_station"):
        lines.append(f"🚇 {_escape(row['nearest_station'])} · {row.get('walk_minutes')} min caminando")

    asking, zone = row.get("price_per_m2_uf"), row.get("zone_price_per_m2_uf")
    if asking and zone:
        delta = (asking / zone - 1) * 100
        mark = "🔻" if delta < 0 else "🔺"
        lines.append(f"📊 {asking:.2f} UF/m² vs {zone:.2f} zona {mark} {abs(delta):.0f}%")

    amenities = [label for column, label in AMENITY_LABELS if row.get(column)]
    if amenities:
        lines.append("✨ " + " · ".join(amenities[:5]))

    if row.get("published_days_ago") is not None:
        lines.append(f"🕐 publicado hace {row['published_days_ago']} días")

    lines.append(f'\n<a href="{_escape(row["url"])}">Ver aviso →</a>')
    return "\n".join(lines)


def send_listing(chat_id: str, text: str) -> None:
    call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
         link_preview_options={"is_disabled": True})
