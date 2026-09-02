"""The settings menu the bot answers `/config` with: every knob, edited from a chat.

The registry in `depas.preferences` already knows what a setting is called, how its
text is parsed and what it means. This module adds the one thing a keyboard needs and
a parser cannot say: how you would rather type it. A weight is six presets, a commune
is a checklist of the forty-three that exist, a metro line is a tier, and only the
handful that are genuinely open -- an address, somebody's user id -- are typed at all.

Deriving the editor from `Setting.parse` rather than from a table of settings is what
keeps the promise the registry makes: adding a knob is adding a `Setting`, and it
arrives in the menu with a keyboard already. `LABELS` and `MENU` are copy and running
order, checked by a test against the registry so a new setting cannot go unreachable.

Everything a press writes goes through `store_preference`, so the chat and the CLI
share one validated write path -- a value the parsers refuse is refused here too, with
the same message, while somebody is still looking at it.
"""
import json
import re
import sqlite3
from datetime import date

from depas.communes import SANTIAGO_PROVINCE
from depas.config import HOME_REQUIRED
from depas.commute import resolve_locations
from depas.detail import MONTH_NAMES
from depas.fetch import Fetcher
from depas.metro import STATION_LINES
from depas.preferences import BY_NAME, Preferences, setting
from depas.store import forget_preference, store_preference
from depas.telegram import answer_callback, ask_value, edit_menu, escape, send_menu
from depas.traits import DISPOSITIONS, EXCLUDE, IGNORE, PENALISE

COMMAND = "/config"
# What Telegram sends when somebody opens a private chat with the bot and presses the
# START button it shows them. It is the first thing a new admin ever sends, so it opens
# the menu too rather than going unanswered.
START = "/start"
# Every callback this module owns starts here, so `bot` can route a press without
# knowing anything about the menu behind it.
PREFIX = "k:"
# Telegram caps callback_data at 64 bytes and silently rejects the whole keyboard past
# it, so a button whose data would not fit is dropped rather than sent.
DATA_LIMIT = 64

DENIED = "no estás en DEPAS_ADMINS"
NO_ADMINS = ("nadie puede configurar el bot desde el chat todavía.\n\nTu id de Telegram "
             "es <code>{user_id}</code>. En la máquina:\n"
             "<code>depas config set DEPAS_ADMINS {user_id}</code>")
NO_AUTHOR = ("un post de canal lo firma el canal, no una persona, así que no hay a quién "
             "autorizar. Escríbeme por privado o comenta en el grupo de discusión.")
STALE = "ese menú quedó viejo; abre /config otra vez"


# ── the menu, as copy ───────────────────────────────────────────────────────────

# What a setting is called in the menu. The emoji is read left to right: what the
# setting is about, then -- for the three numbers that look alike and mean opposite
# things -- whether it is a ceiling (🔺), a floor (🔻) or something to aim at (🎯).
# A weight carries only the first: «Peso ·» already says which of the three it is.
LABELS = {
    "TELEGRAM_CHAT_ID": "📢 Dónde publicar",
    "DEPAS_ADMINS": "👥 Quién configura",
    "DEPAS_COMMUNES": "🗺️ Comunas",
    "DEPAS_BEDROOMS_MIN": "🛏️🔻 Dormitorios mín.",
    "DEPAS_GRADE_MIN": "🏅🔻 Nota mínima",
    "DEPAS_COST_MAX": "💰🔺 Techo de costo",
    "DEPAS_COST_TARGET": "💰🎯 Costo objetivo",
    "DEPAS_COST_WEIGHT": "💰 Peso · costo",
    "DEPAS_PARKING_INCOME": "🚗 Renta estac.",
    "DEPAS_STORAGE_INCOME": "📦 Renta bodega",
    "DEPAS_CURRENT_COST": "🧾 Lo que pagas hoy",
    "DEPAS_CURRENT_HOME": "🏠 Tu depto actual",
    "DEPAS_VALUE_WEIGHT": "📊 Peso · precio zona",
    "DEPAS_WALK_MAX": "🚶🔺 Caminata máx.",
    "DEPAS_WALK_TARGET": "🚶🎯 Caminata ideal",
    "DEPAS_WALK_WEIGHT": "🚶 Peso · caminata",
    "DEPAS_METRO_TIERS": "🚇 Líneas de metro",
    "DEPAS_METRO_WEIGHT": "🚇 Peso · metro",
    "DEPAS_LOCATIONS": "📍 Lugares",
    "DEPAS_COMMUTE_MAX": "🧭🔺 Viaje máx.",
    "DEPAS_COMMUTE_TARGET": "🧭🎯 Viaje ideal",
    "DEPAS_COMMUTE_WEIGHT": "🧭 Peso · viajes",
    "DEPAS_AREA_MIN": "📐🔻 Metraje mín.",
    "DEPAS_AREA_TARGET": "📐🎯 Metraje ideal",
    "DEPAS_AREA_WEIGHT": "📐 Peso · metraje",
    "DEPAS_FLOOR_TARGET": "🛗🎯 Piso ideal",
    "DEPAS_FLOOR_WEIGHT": "🛗 Peso · piso",
    "DEPAS_AGE_TARGET": "🏗️🎯 Antigüedad ideal",
    "DEPAS_AGE_WEIGHT": "🏗️ Peso · antigüedad",
    "DEPAS_AVAILABILITY_TARGET": "📅🎯 Entrega ideal",
    "DEPAS_AVAILABILITY_WEIGHT": "📅 Peso · entrega",
    "DEPAS_AMENITIES_TARGET": "🏊🎯 Comodidades",
    "DEPAS_AMENITIES_WEIGHT": "🏊 Peso · comodidades",
    "DEPAS_SECURITY_WANTED": "🛎️ Conserjería",
    "DEPAS_SECURITY_WEIGHT": "🛎️ Peso · conserjería",
    "DEPAS_FURNISHED": "🛋️ Amoblado",
    "DEPAS_TOP_FLOOR": "🔝 Último piso",
    "DEPAS_TRAITS_WEIGHT": "✨ Peso · características",
}

# Group key, its heading, and the settings it holds in the order they are shown. Every
# setting appears exactly once, which is what lets an editor know where its «Volver»
# goes -- and the weights are all in «Pesos» rather than each beside the parameter it
# scales, because a weight only means anything against the other eleven.
MENU: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("search", "🔍 Búsqueda", ("DEPAS_COMMUNES", "DEPAS_BEDROOMS_MIN", "DEPAS_GRADE_MIN")),
    ("cost", "💰 Costo", ("DEPAS_COST_MAX", "DEPAS_COST_TARGET", "DEPAS_PARKING_INCOME",
                          "DEPAS_STORAGE_INCOME", "DEPAS_CURRENT_COST",
                          "DEPAS_CURRENT_HOME")),
    ("metro", "🚇 Metro", ("DEPAS_WALK_MAX", "DEPAS_WALK_TARGET", "DEPAS_METRO_TIERS")),
    ("commute", "🧭 Viajes", ("DEPAS_LOCATIONS", "DEPAS_COMMUTE_MAX",
                              "DEPAS_COMMUTE_TARGET")),
    ("flat", "🏠 Depto", ("DEPAS_AREA_MIN", "DEPAS_AREA_TARGET", "DEPAS_FLOOR_TARGET",
                          "DEPAS_AGE_TARGET", "DEPAS_AVAILABILITY_TARGET")),
    ("extras", "✨ Extras", ("DEPAS_AMENITIES_TARGET", "DEPAS_SECURITY_WANTED",
                             "DEPAS_FURNISHED", "DEPAS_TOP_FLOOR")),
    ("weights", "⚖️ Pesos", tuple(name for name in BY_NAME if name.endswith("_WEIGHT"))),
    ("bot", "🤖 Bot", ("TELEGRAM_CHAT_ID", "DEPAS_ADMINS")),
)
GROUPS = {key: (heading, names) for key, heading, names in MENU}


# ── what kind of keyboard a setting gets ────────────────────────────────────────
# Keyed on the parser's own name rather than on the setting's, so a new knob inherits
# an editor from the way its text is read. A parser with no kind here is a mistake the
# tests catch, not a setting that quietly falls back to a text box.

NUMBER, WEIGHT, CHOICE, DAY, PLACES, PEOPLE, HOME, COMMUNES, TIERS, PICK = (
    "number", "weight", "choice", "day", "places", "people", "home", "communes",
    "tiers", "pick")
KIND = {"_whole": NUMBER, "_clp": NUMBER, "_number": WEIGHT, "_disposition": CHOICE,
        "_day": DAY, "_locations": PLACES, "_admins": PEOPLE, "_home": HOME,
        "_communes": COMMUNES, "_tiers": TIERS, "_text": PICK}

# Settings counted in pesos, which step by an amount you would actually move a budget
# by rather than by one peso.
MONEY = frozenset({"DEPAS_COST_MAX", "DEPAS_COST_TARGET", "DEPAS_CURRENT_COST",
                   "DEPAS_PARKING_INCOME", "DEPAS_STORAGE_INCOME"})
MONEY_STEPS, PLAIN_STEPS = (25_000, 100_000), (1, 5)
# What a bare number is counted in, so a value read off a button says what it measures.
# Pesos are not here: they are already rendered with their sign.
UNITS = {"DEPAS_WALK_MAX": " min", "DEPAS_WALK_TARGET": " min",
         "DEPAS_COMMUTE_MAX": " min", "DEPAS_COMMUTE_TARGET": " min",
         "DEPAS_AREA_MIN": " m²", "DEPAS_AREA_TARGET": " m²",
         "DEPAS_AGE_TARGET": " años", "DEPAS_FLOOR_TARGET": "º",
         "DEPAS_BEDROOMS_MIN": "D", "DEPAS_AMENITIES_TARGET": " de 9",
         "DEPAS_GRADE_MIN": " pts"}
# What a weight is ever set to in practice: off, half, standard, and the two ways of
# saying "this matters more than the rest".
WEIGHT_PRESETS = ("0", "0.5", "1", "1.5", "2", "3")
# How many months ahead the entrega picker offers, which is as far as a portal ever
# publishes a date.
MONTHS_OFFERED = 6
# The checklist offers the Provincia de Santiago and nothing else: the other eleven RM
# communes the portal indexes are an hour out and would double a list you scroll past
# every time. They are still settable -- typed, like every other open set.
COMMUNES_PER_PAGE = 16
# Three tiers is what a preference between metro lines is ever worth spelling out; a
# line in none of them ranks below all of them, which is what the parser already means.
TIERS_OFFERED = 3
# Every value the code can ever put in `security_type`, so the picker offers it even
# on a database that has not seen one yet.
KNOWN_SECURITY = ("24 horas",)

DISPOSITION_LABELS = {EXCLUDE: "🚫 Excluir", PENALISE: "👎 Castigar", IGNORE: "🙈 Ignorar"}
# The fields of DEPAS_CURRENT_HOME that a stepper can edit, with their steps. lat/lon
# are not here: they are set by typing an address, which is geocoded on the way in.
HOME_FIELDS = (
    ("price_clp", "💵 Arriendo", MONEY_STEPS),
    ("common_expenses", "🧾 Gastos comunes", MONEY_STEPS),
    ("area_m2", "📐 Metraje", PLAIN_STEPS),
    ("bedrooms", "🛏️ Dormitorios", PLAIN_STEPS),
    ("bathrooms", "🚿 Baños", PLAIN_STEPS),
    ("floor", "🛗 Piso", PLAIN_STEPS),
    ("age_years", "🏗️ Antigüedad", PLAIN_STEPS),
    ("parking_spaces", "🚗 Estacionamientos", PLAIN_STEPS),
    ("storage_units", "📦 Bodegas", PLAIN_STEPS),
)
# Held apart from the setting while it is incomplete: DEPAS_CURRENT_HOME only accepts a
# JSON object that already has every required field, so a home built one press at a
# time has nowhere valid to live until the address has been typed.
DRAFT_KEY = "config_home_draft"


def kind(name: str) -> str:
    return KIND[setting(name).parse.__name__]


def steps(name: str) -> tuple[int, int]:
    return MONEY_STEPS if name in MONEY else PLAIN_STEPS


# ── building the keyboards ──────────────────────────────────────────────────────


def _short(name: str) -> str:
    """The setting's name with the prefix every setting shares dropped, to buy bytes."""
    return name.removeprefix("DEPAS_")


def _long(short: str) -> str:
    return short if short in BY_NAME else f"DEPAS_{short}"


def _button(label: str, *parts: object) -> dict[str, str] | None:
    """One button, or None when its callback_data would not survive Telegram's cap."""
    data = PREFIX + ":".join(str(part) for part in parts)
    return None if len(data.encode()) > DATA_LIMIT else {"text": label, "callback_data": data}


def _keyboard(*rows: list[dict[str, str] | None]) -> dict[str, object]:
    """The rows, with dropped buttons and any row they emptied taken out."""
    kept = [[button for button in row if button] for row in rows]
    return {"inline_keyboard": [row for row in kept if row]}


def _money(amount: float) -> str:
    return f"${amount:,.0f}".replace(",", ".")


def _decimal(text: str) -> str:
    """A number as it is written in Spanish, which is not how it is stored."""
    return text.replace(".", ",")


def _shown(name: str, prefs: Preferences) -> str:
    """One setting's value, short enough to ride in a button beside its label."""
    raw, value = prefs.raw(name), prefs.value(name)
    if value is None:
        return "—"
    shape = kind(name)
    if shape == COMMUNES:
        return f"{len(value)} comuna" + ("s" if len(value) != 1 else "")
    if shape == PLACES:
        return f"{len(value)} lugar" + ("es" if len(value) != 1 else "")
    if shape == PEOPLE:
        return f"{len(value)} persona" + ("s" if len(value) != 1 else "")
    if shape == HOME:
        return "definido"
    if shape == CHOICE:
        return DISPOSITION_LABELS[value].split(" ")[1].lower()
    if shape == WEIGHT:
        return _decimal(f"{value:g}")
    if name in MONEY:
        return _money(value)
    if shape == NUMBER:
        return f"{value}{UNITS.get(name, '')}"
    text = raw if raw is not None else str(value)
    return text if len(text) <= 18 else f"{text[:17]}…"


BACK = "⬅️ Volver"
WRITE = "✏️ Escribir"
CLEAR = "🗑️ Borrar"


def _footer(name: str, group: str, clearable: bool = True, writable: bool = True) -> list:
    """The row every editor ends with: type it instead, forget it, or go back."""
    written = ADD if name in SEPARATOR else REPLACE
    return [_button(WRITE, "w", _short(name), written) if writable else None,
            _button(CLEAR, "x", _short(name)) if clearable else None,
            _button(BACK, "g", group)]


def _group_of(name: str) -> str:
    """The group a setting is shown under, which is where its editor goes back to."""
    return next(key for key, _, names in MENU if name in names)


# ── one screen per kind ─────────────────────────────────────────────────────────
# Each returns the text and the keyboard for one setting, given what is configured now.


def _number_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    current = prefs.value(name) or 0
    small, large = steps(name)
    render = _money if name in MONEY else str
    row = []
    for delta in (-large, -small, small, large):
        stepped = max(current + delta, 0)
        sign = "+" if delta > 0 else "−"
        row.append(_button(f"{sign}{render(abs(delta))}", "v", _short(name), stepped))
    return _text(name, prefs), _keyboard(row, _footer(name, group))


def _weight_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    current = prefs.value(name)
    row = [_button(("● " if float(preset) == current else "") + _decimal(preset),
                   "v", _short(name), preset)
           for preset in WEIGHT_PRESETS]
    # Six presets is two comfortable rows on a phone, not one that truncates.
    return _text(name, prefs), _keyboard(row[:3], row[3:], _footer(name, group))


def _choice_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    current = prefs.value(name)
    row = [_button(("● " if choice == current else "") + DISPOSITION_LABELS[choice],
                   "v", _short(name), choice)
           for choice in DISPOSITIONS]
    return _text(name, prefs), _keyboard(row, _footer(name, group))


def _months_ahead(count: int) -> list[date]:
    """The first of each of the next months, which is the granularity an entrega has."""
    today = date.today()
    months = []
    for step in range(1, count + 1):
        ahead = today.month + step - 1  # months since January of this year
        months.append(date(today.year + ahead // 12, ahead % 12 + 1, 1))
    return months


def _day_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    current = prefs.value(name)
    buttons = [_button(("● " if day.isoformat() == current else "")
                       + f"1 {MONTH_NAMES[day.month - 1][:3]}", "v", _short(name),
                       day.isoformat())
               for day in _months_ahead(MONTHS_OFFERED)]
    return _text(name, prefs), _keyboard(buttons[:3], buttons[3:], _footer(name, group))


def _communes_screen(name: str, prefs: Preferences, group: str,
                     page: int = 0) -> tuple[str, dict]:
    chosen = prefs.value(name) or []
    # Whatever is already chosen is offered too, wherever it is: a commune typed in has
    # to be visible, or the checklist would be a place you could not untick it. First,
    # not last -- an unusual choice belongs where you would look for it, not on a page
    # of its own behind the thirty-two you did not pick.
    province = [commune.value for commune in sorted(SANTIAGO_PROVINCE)]
    every = [slug for slug in chosen if slug not in province] + province
    pages = (len(every) + COMMUNES_PER_PAGE - 1) // COMMUNES_PER_PAGE
    page = max(0, min(page, pages - 1))
    shown = every[page * COMMUNES_PER_PAGE:(page + 1) * COMMUNES_PER_PAGE]
    rows = []
    for first in range(0, len(shown), 2):
        rows.append([_button(("✅ " if slug in chosen else "⬜ ") + _pretty(slug),
                             "t", _short(name), slug, page)
                     for slug in shown[first:first + 2]])
    rows.append([_button("◀️", "p", _short(name), page - 1) if page else None,
                 _button(f"{page + 1}/{pages}", "p", _short(name), page),
                 _button("▶️", "p", _short(name), page + 1) if page < pages - 1 else None])
    rows.append(_footer(name, group))
    text = _text(name, prefs)
    if chosen:
        text += "\n\n" + " · ".join(_pretty(slug) for slug in chosen)
    text += "\n\nLa lista es la Provincia de Santiago; el resto de la RM se agrega con ✏️."
    return text, _keyboard(*rows)


def _pretty(slug: str) -> str:
    return slug.replace("-", " ").title()


def _tiers_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    """One row per metro line, its tier ticked -- the whole setting without typing.

    A press does not send the line it moved: it sends the string the setting would then
    hold, rebuilt here. That keeps every write going through the same parser as a value
    typed into the CLI, and keeps one action doing the writing.
    """
    tiers = [list(tier) for tier in (prefs.value(name) or [])]
    tiers += [[] for _ in range(TIERS_OFFERED - len(tiers))]
    lines = sorted({line for calling in STATION_LINES.values() for line in calling})
    rows = []
    for line in lines:
        at = next((index for index, tier in enumerate(tiers) if line in tier), None)
        row = [{"text": f"L{line}", "callback_data": PREFIX + "s:" + _short(name)}]
        for index in range(TIERS_OFFERED):
            row.append(_button(("●" if at == index else "") + str(index + 1),
                               "v", _short(name), _retiered(tiers, line, index)))
        row.append(_button("●—" if at is None else "—", "v", _short(name),
                           _retiered(tiers, line, None)))
        rows.append(row)
    return _text(name, prefs), _keyboard(*rows, _footer(name, group, writable=False))


def _retiered(tiers: list[list[str]], line: str, target: int | None) -> str:
    """The setting's text with one line moved to one tier, or out of all of them."""
    moved = [[held for held in tier if held != line] for tier in tiers]
    if target is not None:
        moved[target].append(line)
    return " > ".join(",".join(sorted(tier)) for tier in moved if tier)


def _places_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    """The places you have to reach, each removable; adding one is typing an address."""
    places = prefs.value(name) or []
    rows = [[_button(f"❌ {place.name}", "d", _short(name), index)]
            for index, place in enumerate(places)]
    rows.append([_button("➕ Agregar lugar", "w", _short(name), "agregar")])
    rows.append(_footer(name, group, writable=False))
    text = _text(name, prefs)
    if places:
        text += "\n\n" + "\n".join(
            f"📍 <b>{escape(place.name)}</b> · {place.lat:.5f}, {place.lon:.5f}"
            for place in places)
    return text, _keyboard(*rows)


def _people_screen(name: str, prefs: Preferences, group: str) -> tuple[str, dict]:
    admins = prefs.value(name) or []
    rows = [[_button(f"❌ {admin}", "d", _short(name), index)]
            for index, admin in enumerate(admins)]
    rows.append([_button("➕ Agregar id", "w", _short(name), "agregar")])
    # No 🗑️: emptying this list is the one edit that cannot be undone from the chat,
    # because there would be nobody left the chat would take an edit from.
    rows.append(_footer(name, group, clearable=False, writable=False))
    return _text(name, prefs), _keyboard(*rows)


def _home_draft(connection: sqlite3.Connection, prefs: Preferences) -> dict:
    """What is configured, else what has been pressed together so far, else nothing."""
    stored = prefs.current_home()
    if stored is not None:
        return dict(stored)
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (DRAFT_KEY,)).fetchone()
    return json.loads(row["value"]) if row else {}


def _keep_draft(connection: sqlite3.Connection, home: dict) -> None:
    connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (DRAFT_KEY, json.dumps(home)))
    connection.commit()


def _home_screen(connection: sqlite3.Connection, prefs: Preferences,
                 group: str) -> tuple[str, dict]:
    """Your own flat field by field, so /compare never asks anybody to type JSON."""
    home = _home_draft(connection, prefs)
    rows = [[_button(f"{label} · {_money(home[field]) if steps_ == MONEY_STEPS else home[field]}"
                     if home.get(field) is not None else f"{label} · —", "h", field)]
            for field, label, steps_ in HOME_FIELDS]
    located = home.get("lat") is not None
    rows.append([_button(("📍 Dirección · puesta" if located else "📍 Dirección · falta"),
                         "w", "CURRENT_HOME", "direccion")])
    rows.append([_button(f"🏙️ Comuna · {_pretty(home['commune'])}" if home.get("commune")
                         else "🏙️ Comuna · —", "hc", 0)])
    rows.append(_footer("DEPAS_CURRENT_HOME", group, writable=False))
    missing = [field for field in HOME_REQUIRED if home.get(field) is None]
    text = _text("DEPAS_CURRENT_HOME", prefs)
    if missing:
        text += ("\n\n⚠️ Falta " + ", ".join(missing)
                 + ", así que todavía no se guarda: /compare lo necesita completo.")
    return text, _keyboard(*rows)


def _home_field_screen(connection: sqlite3.Connection, prefs: Preferences,
                       field: str) -> tuple[str, dict]:
    label, small_large = next((label, steps_) for name, label, steps_ in HOME_FIELDS
                              if name == field)
    small, large = small_large
    home = _home_draft(connection, prefs)
    current = home.get(field) or 0
    render = _money if small_large == MONEY_STEPS else str
    row = [_button(("+" if delta > 0 else "−") + render(abs(delta)), "i", field,
                   max(current + delta, 0))
           for delta in (-large, -small, small, large)]
    text = (f"<b>{label}</b> · tu depto\n\nAhora: "
            f"{render(current) if home.get(field) is not None else '—'}")
    return text, _keyboard(row, [_button(BACK, "s", "CURRENT_HOME")])


def _home_commune_screen(connection: sqlite3.Connection, prefs: Preferences,
                         page: int = 0) -> tuple[str, dict]:
    home = _home_draft(connection, prefs)
    every = [commune.value for commune in sorted(SANTIAGO_PROVINCE)]
    if home.get("commune") and home["commune"] not in every:
        every.append(home["commune"])
    pages = (len(every) + COMMUNES_PER_PAGE - 1) // COMMUNES_PER_PAGE
    page = max(0, min(page, pages - 1))
    shown = every[page * COMMUNES_PER_PAGE:(page + 1) * COMMUNES_PER_PAGE]
    rows = []
    for first in range(0, len(shown), 2):
        rows.append([_button(("● " if slug == home.get("commune") else "") + _pretty(slug),
                             "i", "commune", slug)
                     for slug in shown[first:first + 2]])
    rows.append([_button("◀️", "hc", page - 1) if page else None,
                 _button(f"{page + 1}/{pages}", "hc", page),
                 _button("▶️", "hc", page + 1) if page < pages - 1 else None])
    rows.append([_button(WRITE, "w", "CURRENT_HOME", COMMUNE),
                 _button(BACK, "s", "CURRENT_HOME")])
    text = ("🏙️ <b>Comuna</b> · tu depto\n\nDe aquí sale el precio por m² de la zona "
            "contra el que se compara.\n\nLa lista es la Provincia de Santiago; el resto "
            "de la RM se escribe con ✏️.")
    return text, _keyboard(*rows)


def _options(connection: sqlite3.Connection, name: str, prefs: Preferences) -> list[str]:
    """What a free-text setting can be set to, taken from the data rather than guessed.

    A conserjería is only worth offering if some listing could actually carry it, and a
    chat is only worth offering if the bot has posted in it -- which is exactly what a
    dropdown built out of the database says and a hardcoded list cannot.
    """
    if name == "DEPAS_SECURITY_WANTED":
        seen = [row["security_type"] for row in connection.execute(
            "SELECT DISTINCT security_type FROM listings WHERE security_type IS NOT NULL "
            "ORDER BY security_type")]
        return list(dict.fromkeys(list(KNOWN_SECURITY) + seen))
    if name == "TELEGRAM_CHAT_ID":
        posted = [str(row["chat_id"]) for row in connection.execute(
            "SELECT DISTINCT chat_id FROM card_messages ORDER BY chat_id")]
        current = prefs.raw(name)
        return list(dict.fromkeys(([current] if current else []) + posted))
    return []


def _pick_screen(connection: sqlite3.Connection, name: str, prefs: Preferences,
                 group: str) -> tuple[str, dict]:
    current = prefs.value(name)
    rows = [[_button(("● " if option == current else "") + option, "v", _short(name), option)]
            for option in _options(connection, name, prefs)]
    rows.append(_footer(name, group))
    return _text(name, prefs), _keyboard(*rows)


# ── assembling a screen ─────────────────────────────────────────────────────────


def _text(name: str, prefs: Preferences) -> str:
    """One setting's heading: what it is called here, what it means, what it holds."""
    declared = setting(name)
    source = "" if prefs.is_set(name) else (
        " <i>(por defecto)</i>" if declared.default is not None else " <i>(sin definir)</i>")
    return (f"<b>{LABELS[name]}</b> · <code>{name}</code>\n\n{escape(declared.help)}"
            f"\n\nAhora: <b>{escape(_shown(name, prefs))}</b>{source}")


def main_screen() -> tuple[str, dict]:
    rows = [[_button(heading, "g", key) for key, heading, _ in MENU[first:first + 2]]
            for first in range(0, len(MENU), 2)]
    text = ("⚙️ <b>Configuración</b>\n\nLo que el bot busca y cómo lo puntúa. Cada cambio "
            "se guarda al momento y rige desde la próxima pasada, sin reiniciar nada.")
    return text, _keyboard(*rows)


def group_screen(key: str, prefs: Preferences) -> tuple[str, dict]:
    heading, names = GROUPS[key]
    rows = [[_button(f"{LABELS[name]} · {_shown(name, prefs)}", "s", _short(name))]
            for name in names]
    rows.append([_button("⬅️ Volver", "m")])
    return f"<b>{heading}</b>", _keyboard(*rows)


def setting_screen(connection: sqlite3.Connection, name: str,
                   prefs: Preferences, page: int = 0) -> tuple[str, dict]:
    """The editor for one setting, chosen by the kind its parser implies."""
    group = _group_of(name)
    shape = kind(name)
    if shape == NUMBER:
        return _number_screen(name, prefs, group)
    if shape == WEIGHT:
        return _weight_screen(name, prefs, group)
    if shape == CHOICE:
        return _choice_screen(name, prefs, group)
    if shape == DAY:
        return _day_screen(name, prefs, group)
    if shape == COMMUNES:
        return _communes_screen(name, prefs, group, page)
    if shape == TIERS:
        return _tiers_screen(name, prefs, group)
    if shape == PLACES:
        return _places_screen(name, prefs, group)
    if shape == PEOPLE:
        return _people_screen(name, prefs, group)
    if shape == HOME:
        return _home_screen(connection, prefs, group)
    return _pick_screen(connection, name, prefs, group)


# ── asking for a value that cannot be a button ──────────────────────────────────
# An address, somebody's user id: open sets, so they are typed. The prompt names the
# setting and the action on its first line and the reply quotes it back, which is how a
# typed answer finds its way home without a pending-edit table to go stale.

REPLACE, ADD, ADDRESS, COMMUNE = "reemplazar", "agregar", "direccion", "comuna"
PROMPT_HEAD = re.compile(r"^⚙️ ([A-Z_]+) · (\w+)$")
ASKED = {
    REPLACE: "Responde a este mensaje con el nuevo valor.",
    ADD: "Responde a este mensaje con lo que quieras agregar.",
    ADDRESS: "Responde a este mensaje con la dirección de tu depto; la geocodifico y "
             "guardo las coordenadas.",
    COMMUNE: "Responde a este mensaje con el slug de la comuna donde vives.",
}
EXAMPLES = {ADD: {"DEPAS_LOCATIONS": "pega, Avenida Providencia 1234",
                  "DEPAS_ADMINS": "467291452",
                  "DEPAS_COMMUNES": "puente-alto, san-bernardo"},
            ADDRESS: {"DEPAS_CURRENT_HOME": "Avenida Los Leones 500, Providencia"},
            COMMUNE: {"DEPAS_CURRENT_HOME": "puente-alto"}}


def _prompt(name: str, action: str) -> str:
    declared = setting(name)
    example = EXAMPLES.get(action, {}).get(name) or declared.example
    lines = [f"⚙️ {name} · {action}", "", escape(declared.help), "", ASKED[action]]
    if example:
        lines.append(f"Ejemplo: <code>{escape(example)}</code>")
    return "\n".join(lines)


# ── handling what somebody pressed or typed ─────────────────────────────────────


def _author(message: dict) -> int | None:
    """Who sent this, or None -- a channel post is signed by the channel, not a person."""
    return (message.get("from") or {}).get("id")


def open_menu(connection: sqlite3.Connection, message: dict, prefs: Preferences) -> None:
    """Answer /config with the menu, or with why this particular sender cannot have it."""
    chat, thread = str(message["chat"]["id"]), message.get("message_thread_id")
    user_id = _author(message)
    if user_id is None:
        send_menu(chat, NO_AUTHOR, None, thread, message["message_id"])
        return
    if not prefs.is_admin(user_id):
        # Telling somebody their own id is the whole bootstrap: it is what they paste
        # into `depas config set DEPAS_ADMINS`, or send to whoever already is one.
        send_menu(chat, NO_ADMINS.format(user_id=user_id), None, thread,
                  message["message_id"])
        return
    text, keyboard = main_screen()
    send_menu(chat, text, keyboard, thread, message["message_id"])


def _redraw(connection: sqlite3.Connection, callback: dict, text: str, keyboard: dict) -> None:
    message = callback.get("message") or {}
    if not message:
        return  # a press old enough that Telegram no longer sends what it was on
    edit_menu(str(message["chat"]["id"]), message["message_id"], text, keyboard)


def _write(connection: sqlite3.Connection, name: str, raw: str) -> str:
    """Store one value, answering with what to say in the toast either way."""
    try:
        store_preference(connection, name, raw)
    except ValueError as error:
        # A parser quotes back what was typed, and this string is shown both as a plain
        # toast and inside a screen. Escaped here, so every caller can treat it as HTML.
        return f"⚠️ {escape(str(error))}"
    return "✅ guardado"


def press(connection: sqlite3.Connection, callback: dict, prefs: Preferences) -> None:
    """One press on the config keyboard, authorised against the whitelist every time.

    Checked per press rather than only when the menu is opened: the menu is a message,
    and in a group anybody can reach the buttons on somebody else's.
    """
    data = (callback.get("data") or "").removeprefix(PREFIX)
    if not prefs.is_admin(_author(callback)):
        answer_callback(callback["id"], DENIED)
        return
    action, _, rest = data.partition(":")
    try:
        toast = _act(connection, callback, action, rest)
    except (KeyError, ValueError, StopIteration, IndexError):
        # A keyboard from before a deploy that renamed or regrouped something. Saying so
        # beats a traceback per press and a menu that never redraws.
        answer_callback(callback["id"], STALE)
        return
    answer_callback(callback["id"], toast)


def _act(connection: sqlite3.Connection, callback: dict, action: str, rest: str) -> str:
    """Do what one press asks and redraw the menu under it; return the toast to show."""
    prefs = Preferences.load(connection)
    if action == "m":
        _redraw(connection, callback, *main_screen())
        return ""
    if action == "g":
        _redraw(connection, callback, *group_screen(rest, prefs))
        return ""
    if action == "s":
        _redraw(connection, callback, *setting_screen(connection, _long(rest), prefs))
        return ""
    if action == "p":
        short, _, page = rest.partition(":")
        _redraw(connection, callback, *setting_screen(connection, _long(short), prefs, int(page)))
        return ""
    if action == "v":
        short, _, value = rest.partition(":")
        return _set(connection, callback, _long(short), value)
    if action == "x":
        return _clear(connection, callback, _long(rest))
    if action == "t":
        return _toggle(connection, callback, rest)
    if action == "d":
        return _drop(connection, callback, rest)
    if action == "w":
        return _ask(connection, callback, rest, prefs)
    if action == "h":
        _redraw(connection, callback, *_home_field_screen(connection, prefs, rest))
        return ""
    if action == "hc":
        _redraw(connection, callback, *_home_commune_screen(connection, prefs, int(rest)))
        return ""
    if action == "i":
        return _set_home_field(connection, callback, rest)
    raise ValueError(f"unknown config action {action!r}")


def _set(connection: sqlite3.Connection, callback: dict, name: str, value: str) -> str:
    toast = _write(connection, name, value)
    prefs = Preferences.load(connection)
    _redraw(connection, callback, *setting_screen(connection, name, prefs))
    return toast


def _clear(connection: sqlite3.Connection, callback: dict, name: str) -> str:
    forget_preference(connection, name)
    prefs = Preferences.load(connection)
    _redraw(connection, callback, *setting_screen(connection, name, prefs))
    return "🗑️ borrado; vuelve a su valor por defecto"


def _toggle(connection: sqlite3.Connection, callback: dict, rest: str) -> str:
    """Add or remove one item of a list setting, which is how a checklist writes."""
    short, item, page = rest.split(":")
    name = _long(short)
    prefs = Preferences.load(connection)
    chosen = list(prefs.value(name) or [])
    chosen.remove(item) if item in chosen else chosen.append(item)
    toast = _write(connection, name, ",".join(chosen))
    _redraw(connection, callback, *setting_screen(connection, name,
                                                  Preferences.load(connection), int(page)))
    return toast


LAST_ADMIN = "no puedo dejar la lista vacía: nadie podría volver a configurar desde el chat"
# The list settings, and what joins their entries. Being in here is what makes an
# editor append what is typed rather than replace everything with it.
SEPARATOR = {"DEPAS_LOCATIONS": "; ", "DEPAS_ADMINS": ",", "DEPAS_COMMUNES": ","}


def _drop(connection: sqlite3.Connection, callback: dict, rest: str) -> str:
    """Remove the nth entry of a list somebody built by typing, by its position."""
    short, _, index = rest.partition(":")
    name = _long(short)
    prefs = Preferences.load(connection)
    entries = [entry.strip() for entry in
               (prefs.raw(name) or "").split(SEPARATOR[name].strip()) if entry.strip()]
    if name == "DEPAS_ADMINS" and len(entries) <= 1:
        return LAST_ADMIN
    entries.pop(int(index))
    toast = _write(connection, name, SEPARATOR[name].join(entries))
    _redraw(connection, callback, *setting_screen(connection, name, Preferences.load(connection)))
    return toast


def _ask(connection: sqlite3.Connection, callback: dict, rest: str,
         prefs: Preferences) -> str:
    """Post the force-reply prompt for a value no keyboard can offer."""
    short, _, action = rest.partition(":")
    name = _long(short)
    message = callback.get("message") or {}
    ask_value(str(message["chat"]["id"]), _prompt(name, action or REPLACE),
              message.get("message_thread_id"))
    return "✏️ responde al mensaje de abajo"


def _set_home_field(connection: sqlite3.Connection, callback: dict, rest: str) -> str:
    """One field of your own flat, stepped or picked rather than typed as JSON."""
    field, _, value = rest.partition(":")
    prefs = Preferences.load(connection)
    home = _home_draft(connection, prefs)
    home[field] = value if field == "commune" else float(value)
    if field != "commune" and home[field] == int(home[field]):
        home[field] = int(home[field])
    toast = _keep_home(connection, home)
    prefs = Preferences.load(connection)
    if field == "commune":
        _redraw(connection, callback, *_home_commune_screen(connection, prefs))
    else:
        _redraw(connection, callback, *_home_field_screen(connection, prefs, field))
    return toast


def _keep_home(connection: sqlite3.Connection, home: dict) -> str:
    """Promote the draft to the setting once it has everything /compare needs.

    Held back until then because the setting refuses a half-filled home -- correctly, it
    is what the comparison reads -- and a menu that writes field by field would otherwise
    have nowhere to put the first one.
    """
    missing = [field for field in HOME_REQUIRED if home.get(field) is None]
    if missing:
        _keep_draft(connection, home)
        return f"falta {', '.join(missing)} para guardarlo"
    toast = _write(connection, "DEPAS_CURRENT_HOME", json.dumps(home))
    connection.execute("DELETE FROM settings WHERE key = ?", (DRAFT_KEY,))
    connection.commit()
    return toast


def answer_prompt(connection: sqlite3.Connection, fetcher: Fetcher, message: dict,
                  prefs: Preferences) -> bool:
    """Take a typed value if this message is a reply to one of our prompts.

    Returns whether it was: a message that is not an answer to a prompt has to fall
    through to everything else the bot reads a message for.
    """
    replied = message.get("reply_to_message") or {}
    head = PROMPT_HEAD.match((replied.get("text") or "").split("\n")[0])
    if not head:
        return False
    name, action = head.group(1), head.group(2)
    if name not in BY_NAME:
        return False
    chat, thread = str(message["chat"]["id"]), message.get("message_thread_id")
    if not prefs.is_admin(_author(message)):
        send_menu(chat, DENIED, None, thread, message["message_id"])
        return True
    typed = (message.get("text") or "").strip()
    try:
        toast = _typed(connection, fetcher, name, action, typed, prefs)
    except (ValueError, RuntimeError) as error:
        # Everything a parser or the geocoder refuses, said where it was typed rather
        # than as a toast that has nothing to hang off.
        send_menu(chat, f"⚠️ {escape(str(error))}", None, thread, message["message_id"])
        return True
    text, keyboard = setting_screen(connection, name, Preferences.load(connection))
    send_menu(chat, f"{toast}\n\n{text}", keyboard, thread, message["message_id"])
    return True


def _typed(connection: sqlite3.Connection, fetcher: Fetcher, name: str, action: str,
           typed: str, prefs: Preferences) -> str:
    """Apply one typed value: replacing the setting, appending to it, or geocoding it."""
    if action == COMMUNE:
        # Validated by the commune parser rather than by a second list of slugs: one
        # place knows what a commune is called, and it is already the one that says so.
        setting("DEPAS_COMMUNES").parse("La comuna", typed)
        home = _home_draft(connection, prefs) | {"commune": typed.strip()}
        return _keep_home(connection, home)
    if action == ADDRESS:
        lat, lon, where = _coordinates(fetcher, typed)
        home = _home_draft(connection, prefs) | {"lat": lat, "lon": lon}
        return f"📍 {escape(where)}\n{_keep_home(connection, home)}"
    if action == ADD:
        separator = SEPARATOR[name]
        existing = [entry.strip() for entry in (prefs.raw(name) or "").split(separator.strip())]
        added = [entry.strip() for entry in typed.split(separator.strip())]
        # Deduplicated: typing a commune already ticked would otherwise double it, and a
        # list setting means the same thing either way -- so it should read the same too.
        typed = separator.join(dict.fromkeys(entry for entry in existing + added if entry))
    if name == "DEPAS_LOCATIONS":
        # An address is a way of typing coordinates, resolved once on the way in so the
        # table keeps what routing actually wants. Same call the CLI makes.
        typed, matched = resolve_locations(fetcher, typed)
        if matched:
            return "📍 " + escape(" · ".join(matched)) + "\n" + _write(connection, name, typed)
    return _write(connection, name, typed)


def _coordinates(fetcher: Fetcher, address: str) -> tuple[float, float, str]:
    """Your own flat's coordinates from an address, through the one geocoder there is."""
    resolved, matched = resolve_locations(fetcher, f"casa, {address}")
    _, lat, lon = resolved.split(",")
    return float(lat), float(lon), matched[0].removeprefix("casa → ") if matched else address


__all__ = ["COMMAND", "GROUPS", "KIND", "LABELS", "MENU", "PREFIX", "START",
           "answer_prompt",
           "group_screen", "kind", "main_screen", "open_menu", "press", "setting_screen"]
