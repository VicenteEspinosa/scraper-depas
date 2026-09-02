"""Every tunable the bot has, declared once and read from the database.

Settings used to be read straight out of `os.environ` wherever they happened to be
needed, which meant the environment was the configuration: changing a preference was
editing a file on the box and restarting. They live in the `preferences` table now,
and this module is the single place that knows what a setting is called, how its text
is parsed, what it means and what it falls back to when nobody has said.

Three readers share that one declaration: the .env file the table is seeded from, the
`depas config` commands that edit it, and the chat commands that will. Adding a knob is
adding a `Setting` below.

Nothing here reaches for a global: a `Preferences` is a snapshot somebody hands you,
which is what lets one process eventually hold several of them at once.

Two languages, on purpose and along one line: `help` is copy, shown to whoever is
editing a setting from the chat, so it reads the way the bot's replies do. Everything
raised is an error, which surfaces in a log or a traceback, so it reads the way the
rest of the codebase does.
"""
import difflib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from depas.communes import Commune
from depas.config import (DEFAULT_TARGET_AGE, HOME_REQUIRED, Location, defaults,
                          environment)
from depas.traits import DISPOSITIONS, PENALISE, TRAITS

# ── how a setting's text becomes a value ────────────────────────────────────────
# Every parser takes the raw string and either returns the value or raises ValueError
# with a message worth showing to whoever typed it -- in .env, in the CLI or in the
# chat. The message names the setting, because by the time it surfaces the reader has
# no idea which one was being read.


def _whole(name: str, raw: str) -> int:
    if not raw.lstrip("-").isdigit():
        raise ValueError(f"{name} must be a whole number, got {raw!r}")
    return int(raw)


def _clp(name: str, raw: str) -> int:
    if not raw.isdigit():
        raise ValueError(f"{name} must be a whole number of CLP, got {raw!r}")
    return int(raw)


def _number(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _text(name: str, raw: str) -> str:
    return raw


def _day(name: str, raw: str) -> str:
    """An ISO date, kept as text: the columns it is compared against are text too."""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date as YYYY-MM-DD, got {raw!r}") from None


def _communes(name: str, raw: str) -> list[str]:
    slugs = [slug.strip() for slug in raw.split(",") if slug.strip()]
    known = {commune.value for commune in Commune}
    unknown = [slug for slug in slugs if slug not in known]
    if unknown:
        raise ValueError(f"{name} does not know the commune {', '.join(unknown)}; "
                         "slugs look like `nunoa` or `estacion-central`")
    return slugs


def _admins(name: str, raw: str) -> list[int]:
    """Telegram user ids allowed to configure the bot from a chat.

    Ids rather than @usernames on purpose: a username can be changed, and once freed it
    can be claimed by somebody else, so a whitelist keyed on one is a whitelist that
    quietly changes hands.
    """
    entries = [entry.strip().lstrip("@") for entry in raw.split(",") if entry.strip()]
    wrong = [entry for entry in entries if not entry.isdigit()]
    if wrong:
        raise ValueError(f"{name} takes numeric Telegram user ids, got {', '.join(wrong)}; "
                         "a user id is a positive number, not a username")
    return [int(entry) for entry in entries]


def _locations(name: str, raw: str) -> list[Location]:
    found = []
    for entry in raw.split(";"):
        if not entry.strip():
            continue
        parts = [part.strip() for part in entry.split(",")]
        if len(parts) != 3:
            raise ValueError(f"{name} entry must be name,lat,lon: {entry!r}")
        label, lat, lon = parts
        try:
            found.append(Location(label, float(lat), float(lon)))
        except ValueError:
            raise ValueError(f"{name} entry must be name,lat,lon: {entry!r}") from None
    return found


def _tiers(name: str, raw: str) -> list[list[str]]:
    """Metro lines in tiers, best first; lines sharing a tier are worth the same."""
    tiers = [[line.strip().upper() for line in tier.split(",") if line.strip()]
             for tier in raw.split(">")]
    return [tier for tier in tiers if tier]


def _disposition(name: str, raw: str) -> str:
    """What a trait does to a listing that has it, which is the only thing a trait sets."""
    choice = raw.strip().lower()
    if choice not in DISPOSITIONS:
        raise ValueError(f"{name} must be one of {', '.join(DISPOSITIONS)}, got {raw!r}")
    return choice


def _home(name: str, raw: str) -> dict:
    try:
        home = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must be a single JSON object: {error}") from None
    if not isinstance(home, dict):
        raise ValueError(f"{name} must be a single JSON object, not a {type(home).__name__}")
    missing = [field for field in HOME_REQUIRED if home.get(field) is None]
    if missing:
        raise ValueError(f"{name} is missing {', '.join(missing)}")
    return home


# ── the registry ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Setting:
    """One tunable: what it is called, how its text is read, and what it means.

    `default` is the raw text an unset setting behaves as, or None when unset means
    the feature is simply off -- most of them, because a target nobody set must not
    invent an opinion.
    """

    name: str
    parse: Callable[[str, str], object]
    help: str
    example: str = ""
    default: str | None = None

    def value(self, raw: str | None) -> object | None:
        text = raw if raw is not None else self.default
        return None if text is None else self.parse(self.name, text)


# Every component that carries a weight, named for what it measures rather than for a
# category: `walk` is minutes to the metro and `area` is square metres, which is what
# DEPAS_WALK_* and DEPAS_AREA_* configure.
WEIGHTED = ("value", "cost", "walk", "area", "amenities", "security", "floor",
            "metro", "commute", "age", "availability", "traits")


def _trait_settings() -> list[Setting]:
    """One setting per trait, declared from the same registry the scoring reads."""
    return [Setting(trait.setting, _disposition, trait.help,
                    example=trait.default, default=trait.default)
            for trait in TRAITS]


def _weight(component: str) -> Setting:
    """The weight slot every graded component has, declared beside the rest of its slots."""
    return Setting(f"DEPAS_{component.upper()}_WEIGHT", _number,
                   f"Peso relativo del componente «{component}» en la nota.",
                   example="1", default="1")


# Named DEPAS_<PARAMETER>_<SLOT>, so every knob for one parameter sorts together and
# the slot says what it does to a listing:
#   MIN / MAX  a hard bound -- outside it there is no alert at all
#   TARGET     an ideal -- being short of it costs score and nothing else
#   WEIGHT     how much that component moves the final grade
#   WANTED     a value to match, scored on equality
#   TIERS      a ranked preference, best first
SETTINGS: tuple[Setting, ...] = (
    Setting("TELEGRAM_CHAT_ID", _text,
            "Dónde se publican las alertas: un canal (cada tarjeta con sus comentarios) "
            "o un grupo. `depas chats` lista lo que el bot ve.",
            example="-1001234567890"),
    Setting("DEPAS_ADMINS", _admins,
            "Quiénes pueden cambiar la configuración desde el chat, como ids numéricos "
            "de Telegram separados por coma. Vacío es nadie: los ajustes solo se editan "
            "con `depas config` en la máquina.",
            example="467291452"),

    # -- what is even looked at ---------------------------------------------------
    Setting("DEPAS_COMMUNES", _communes,
            "Comunas que revisa la pasada horaria, como slugs separados por coma.",
            example="nunoa,santiago"),
    Setting("DEPAS_BEDROOMS_MIN", _whole,
            "Mínimo de dormitorios. Se aplica al buscar y otra vez al alertar.",
            example="2"),
    Setting("DEPAS_GRADE_MIN", _whole,
            "Nota mínima para publicar una tarjeta. La nota mide qué tanto se cumplen "
            "tus preferencias, así que 100 es cumplirlas todas y se puede pasar.",
            example="85"),

    # -- cost ---------------------------------------------------------------------
    Setting("DEPAS_COST_MAX", _whole,
            "Techo duro del costo neto mensual: por encima no hay alerta. De aquí sale "
            "también el tope de arriendo que se usa al crawlear.",
            example="950000"),
    Setting("DEPAS_COST_TARGET", _whole,
            "Lo que apuntamos a gastar al mes, neto. En o bajo esto la nota de costo es "
            "máxima; sobre esto baja hasta cero en el techo, sin excluir nada.",
            example="850000"),
    _weight("cost"),

    # -- walk to the metro ---------------------------------------------------------
    Setting("DEPAS_WALK_MAX", _whole,
            "Máximos minutos caminando al metro: por encima no hay alerta.",
            example="15"),
    Setting("DEPAS_WALK_TARGET", _whole,
            "Caminata ideal al metro. En o bajo esto la nota de caminata es máxima.",
            example="10"),
    _weight("walk"),

    # -- area ----------------------------------------------------------------------
    Setting("DEPAS_AREA_MIN", _whole,
            "Piso duro de metraje, pero un aviso que no publica superficie igual alerta: "
            "solo puntúa al fondo del componente.",
            example="42"),
    Setting("DEPAS_AREA_TARGET", _whole,
            "Metraje ideal. En o sobre esto la nota de tamaño es máxima.",
            example="50"),
    _weight("area"),

    # -- commute -------------------------------------------------------------------
    Setting("DEPAS_COMMUTE_MAX", _whole,
            "Máximos minutos de viaje al lugar peor conectado: por encima no hay alerta.",
            example="60"),
    Setting("DEPAS_COMMUTE_TARGET", _whole,
            "Viaje ideal en minutos al lugar peor conectado de DEPAS_LOCATIONS.",
            example="25"),
    _weight("commute"),
    Setting("DEPAS_LOCATIONS", _locations,
            "Cada lugar al que tienes que poder llegar, como `nombre,lat,lon` separados "
            "por `;`. El nombre es la etiqueta que imprimen las tarjetas.",
            example="pega,-33.41720,-70.60600; gimnasio,-33.49830,-70.61140"),

    # -- the rest of the graded parameters ------------------------------------------
    Setting("DEPAS_FLOOR_TARGET", _whole,
            "Piso ideal. Más abajo puntúa peor sin excluirse, y el último piso se castiga "
            "por el techo que tiene encima. Nunca es un corte.",
            example="5"),
    _weight("floor"),
    Setting("DEPAS_AGE_TARGET", _whole,
            "Antigüedad ideal en años. Es el único objetivo que rige aun sin configurarse: "
            "borrarlo no apaga la preferencia, vuelve al estándar de 25.",
            example="25", default=str(DEFAULT_TARGET_AGE)),
    _weight("age"),
    Setting("DEPAS_AVAILABILITY_TARGET", _day,
            "Fecha de entrega ideal. Puntúa por cercanía a esa fecha en cualquiera de las "
            "dos direcciones, así que entregar meses antes tampoco es gratis. Nunca es un "
            "corte, y un aviso que no declara fecha queda sin puntuar.",
            example="2026-11-01"),
    _weight("availability"),
    Setting("DEPAS_SECURITY_WANTED", _text,
            "Conserjería buscada. No es un corte: quien no la declara puntúa más bajo.",
            example="24 horas"),
    _weight("security"),
    Setting("DEPAS_METRO_TIERS", _tiers,
            "Líneas de metro por tramos, mejor primero: `>` separa tramos y `,` lista "
            "líneas que valen lo mismo. Una línea que no aparece va bajo todas.",
            example="1 > 3,6 > 2,4,4A,5"),
    _weight("metro"),

    # Graded off the listing alone, so it weighs something without configuring anything.
    _weight("value"),
    Setting("DEPAS_AMENITIES_TARGET", _whole,
            "Cuántas comodidades esperas encontrar, de nueve. En ese número la nota del "
            "componente es máxima; más suman igual. En cero el componente se apaga.",
            example="4", default="4"),
    _weight("amenities"),

    # -- yes/no properties, each either a deal-breaker or a dislike --------------------
    *_trait_settings(),
    _weight("traits"),

    # -- what a listing is priced and compared against --------------------------------
    Setting("DEPAS_PARKING_INCOME", _clp,
            "CLP al mes que esperas cobrar arrendando el estacionamiento.",
            example="60000", default="0"),
    Setting("DEPAS_STORAGE_INCOME", _clp,
            "CLP al mes que esperas cobrar arrendando la bodega.",
            example="30000", default="0"),
    Setting("DEPAS_CURRENT_COST", _whole,
            "Lo que pagas hoy, neto. Déjalo vacío si definiste DEPAS_CURRENT_HOME: de ahí "
            "se calcula solo.",
            example="930000"),
    Setting("DEPAS_CURRENT_HOME", _home,
            "Tu propio depto, para que /compare pueda medir cualquier aviso contra él. "
            "Desde el chat se arma campo por campo; en la CLI es un objeto JSON. "
            f"Obligatorios: {', '.join(HOME_REQUIRED)}.",
            example='{"commune":"nunoa","price_clp":800000,"common_expenses":130000,'
                    '"area_m2":62,"lat":-33.45590,"lon":-70.59780}'),

)

BY_NAME: Mapping[str, Setting] = {setting.name: setting for setting in SETTINGS}

# Read before a database can be opened, or too sensitive to sit in it, so these stay
# in the environment and are not settings. Named here so a check over .env can tell
# them apart from a key somebody misspelled.
BOOTSTRAP = frozenset({"DEPAS_DB_PATH", "TELEGRAM_BOT_TOKEN"})
CONFIGURABLE_PREFIXES = ("DEPAS_", "TELEGRAM_")


def check_environment() -> tuple[int, list[str]]:
    """Parse every setting .env declares, reporting what a seed would refuse or ignore.

    Worth its own pass because a value only reaches the table through a parser: a .env
    that no longer validates stops the process at `connect`, which on a box that
    restarts its containers is a crash loop rather than an error somebody reads. Run
    this before restarting anything, and the deploy fails instead of the bot.
    """
    found = environment()
    problems, checked = [], 0
    for declared in SETTINGS:
        raw = found.get(declared.name, "").strip()
        if not raw:
            continue
        checked += 1
        try:
            declared.parse(declared.name, raw)
        except ValueError as error:
            problems.append(str(error))
    # The quieter failure: a misspelled or renamed key is not refused, it is skipped,
    # and the setting it was meant to be simply never turns on.
    problems += [f"{name} is not a setting, so it would be ignored"
                 for name in sorted(found)
                 if name.startswith(CONFIGURABLE_PREFIXES)
                 and name not in BY_NAME and name not in BOOTSTRAP]
    return checked, problems


def setting(name: str) -> Setting:
    """The declaration for one setting, or a ValueError naming the likeliest typo."""
    found = BY_NAME.get(name)
    if found is not None:
        return found
    # A misremembered name is the common case, in the CLI and even more so in a chat,
    # so the error is worth more than "unknown": it should say what was probably meant.
    near = difflib.get_close_matches(name.upper(), BY_NAME, n=3, cutoff=0.6)
    if not near:
        near = sorted(known for known in BY_NAME if name.upper() in known)[:3]
    hint = f"; did you mean {', '.join(near)}?" if near else ""
    raise ValueError(f"{name} is not a setting{hint}")


# ── a snapshot of what is configured ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Bounds:
    """One numeric parameter's three slots, already parsed.

    `minimum` and `maximum` exclude a listing outright; `target` only costs it score.
    Any of them being None means that slot was never configured, which is why the
    scorers can take a Bounds without asking whether it is complete.
    """

    minimum: int | None = None
    target: int | None = None
    maximum: int | None = None


class Preferences:
    """What one reader wants, as raw text per setting, parsed on demand and cached.

    Deliberately a value rather than a lookup into somewhere global: everything that
    grades, filters or renders takes one of these, so the day there are several
    readers the only thing that changes is which snapshot gets passed in.
    """

    __slots__ = ("_raw", "_parsed", "cost", "walk", "area", "commute", "age", "floor")

    def __init__(self, raw: Mapping[str, str]) -> None:
        # Validated here rather than at first read: a typo in a setting nothing happens
        # to touch this pass is still a typo, and saying so at load is saying so early.
        self._raw = {name: text for name, text in raw.items() if text != ""}
        self._parsed: dict[str, object | None] = {}
        for name in self._raw:
            self.value(name)
        # Built once, from the one query that filled `raw`: the scorers run per listing
        # per component, so they read an attribute rather than re-parsing a name.
        self.cost = self._bounds("COST")
        self.walk = self._bounds("WALK")
        self.area = self._bounds("AREA")
        self.commute = self._bounds("COMMUTE")
        self.age = self._bounds("AGE")
        self.floor = self._bounds("FLOOR")

    def _bounds(self, parameter: str) -> Bounds:
        """The MIN/TARGET/MAX a parameter declares; slots it has no setting for stay None."""
        return Bounds(*(self.value(f"DEPAS_{parameter}_{slot}")
                        if f"DEPAS_{parameter}_{slot}" in BY_NAME else None
                        for slot in ("MIN", "TARGET", "MAX")))

    def __repr__(self) -> str:
        return f"Preferences({len(self._raw)} set)"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, str]) -> "Preferences":
        """A snapshot built straight from raw text, for tests and for one-off passes."""
        return cls({name: text for name, text in raw.items() if name in BY_NAME})

    @classmethod
    def from_env(cls) -> "Preferences":
        """What .env and the exported environment say, which is what seeds the table."""
        found = environment()
        return cls.from_mapping({declared.name: found[declared.name]
                                 for declared in SETTINGS if declared.name in found})

    @classmethod
    def load(cls, connection: sqlite3.Connection) -> "Preferences":
        """What the database says, which is what everything actually runs on."""
        return cls.from_mapping(
            {row["name"]: row["value"] for row in connection.execute(
                "SELECT name, value FROM preferences")}
        )

    # -- reading -----------------------------------------------------------------

    def raw(self, name: str) -> str | None:
        """The text somebody configured, or None where the default is standing in."""
        setting(name)
        return self._raw.get(name)

    def is_set(self, name: str) -> bool:
        return setting(name).name in self._raw

    def value(self, name: str) -> object | None:
        """One setting, parsed: its configured text, else its default, else None."""
        if name not in self._parsed:
            self._parsed[name] = setting(name).value(self._raw.get(name))
        return self._parsed[name]

    # -- the handful of settings that mean something together --------------------

    def lease_income(self, kind: str) -> int:
        """Monthly CLP a parking space or storage unit is expected to earn."""
        return self.value(f"DEPAS_{kind.upper()}_INCOME")

    def weights(self) -> dict[str, float]:
        weights = {name: self.value(f"DEPAS_{name.upper()}_WEIGHT") for name in WEIGHTED}
        if sum(weights.values()) <= 0:
            raise ValueError("at least one DEPAS_*_WEIGHT must be positive")
        return weights

    def locations(self) -> list[Location]:
        """Every place you have to be able to reach from the apartment."""
        return self.value("DEPAS_LOCATIONS") or []

    def metro_tiers(self) -> list[list[str]]:
        return self.value("DEPAS_METRO_TIERS") or []

    def communes(self) -> list[str]:
        return self.value("DEPAS_COMMUNES") or []

    def security_wanted(self) -> str | None:
        return self.value("DEPAS_SECURITY_WANTED")

    def traits(self, disposition: str) -> list:
        """Every trait currently set to `exclude`, or to `penalise`."""
        return [trait for trait in TRAITS if self.value(trait.setting) == disposition]

    def penalised_traits(self) -> list:
        return self.traits(PENALISE)

    def chat_id(self) -> str:
        """The Telegram chat alerts are posted to."""
        found = self.value("TELEGRAM_CHAT_ID")
        if not found:
            raise ValueError("set TELEGRAM_CHAT_ID (run `depas chats` to find it)")
        return found

    def admins(self) -> list[int]:
        """Telegram user ids allowed to edit the settings from a chat."""
        return self.value("DEPAS_ADMINS") or []

    def is_admin(self, user_id: int | None) -> bool:
        """Whether one Telegram user may configure the bot from a chat -- nobody by default.

        Deliberately not "anybody in the alert chat": a channel's discussion group is
        joinable, so being able to reach the bot is not the same as being trusted with
        what it looks for. A message with no author at all -- a channel post is signed by
        the channel rather than by a person -- can never pass.
        """
        return user_id is not None and user_id in self.admins()

    def current_home(self) -> dict | None:
        """Your own apartment, or None when you have not described it."""
        return self.value("DEPAS_CURRENT_HOME")

    def home_net_monthly_clp(self, home: dict) -> int:
        """What your place costs a month, priced on the same terms as a listing."""
        return round(home["price_clp"] + home["common_expenses"]
                     - (home.get("parking_spaces") or 0) * self.lease_income("parking")
                     - (home.get("storage_units") or 0) * self.lease_income("storage"))

    def current_cost(self) -> int | None:
        """What you pay now, net, so every listing can be shown as a difference."""
        configured = self.value("DEPAS_CURRENT_COST")
        if configured is not None:
            return configured
        home = self.current_home()
        return None if home is None else self.home_net_monthly_clp(home)

    def max_rent(self) -> int | None:
        """Rent ceiling for the crawl, derived from the budget rather than configured.

        Gastos comunes only add to the net cost and sublet income is the only thing that
        subtracts, so rent above budget-plus-maximum-sublet can never come in under budget.
        """
        budget = self.cost.maximum
        if budget is None:
            return None
        return budget + 2 * self.lease_income("parking") + self.lease_income("storage")


# ── writing ─────────────────────────────────────────────────────────────────────

# Recorded in `settings`, which is the internal key/value scratch the code writes to
# itself -- the Telegram offset, the mirrored sublet income -- as opposed to
# `preferences`, which is what a person configures.
SEEDED_KEY = "preferences_seeded"


def set_preference(connection: sqlite3.Connection, name: str, raw: str) -> object | None:
    """Store one setting after checking it parses, and return what it now means.

    Validating before writing is the whole point of a registry: a value that would only
    blow up on the next watch pass is refused here, while somebody is still looking.
    """
    declared = setting(name)
    text = raw.strip()
    if text == "":
        clear_preference(connection, name)
        return declared.value(None)
    value = declared.parse(name, text)
    connection.execute(
        "INSERT INTO preferences (name, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (name, text, datetime.now(UTC).isoformat()),
    )
    connection.commit()
    return value


def clear_preference(connection: sqlite3.Connection, name: str) -> None:
    """Forget one setting, so it falls back to its default -- or to being off."""
    setting(name)
    connection.execute("DELETE FROM preferences WHERE name = ?", (name,))
    connection.commit()


def seed_from_env(connection: sqlite3.Connection, force: bool = False) -> list[str]:
    """Copy seed.env and the environment into the table once, so a box keeps its settings.

    Only ever the first time: after that the database is the configuration and .env is
    history, or a preference cleared from the chat would come back on the next restart.
    `force` is the deliberate re-import behind `depas config import-env`.
    """
    already = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (SEEDED_KEY,)).fetchone()
    if already and not force:
        return []
    # The checked-in defaults underneath, whatever this box says on top.
    found = defaults() | environment()
    seeded = []
    for declared in SETTINGS:
        text = found.get(declared.name, "").strip()
        if not text:
            continue
        # Parsed before it is stored, so a typo in .env is still loud -- it just says so
        # on the first connect rather than on the first read.
        declared.parse(declared.name, text)
        set_preference(connection, declared.name, text)
        seeded.append(declared.name)
    connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value = 1", (SEEDED_KEY,))
    connection.commit()
    return seeded


# Where a value came from, as a token rather than a phrase: the CLI and the chat
# print it in their own words, and neither has to guess by comparing against defaults.
SET, DEFAULTED, UNSET = "set", "default", "unset"


def described(preferences: Preferences) -> list[tuple[Setting, str | None, str]]:
    """Every setting with what it is set to and where that value came from."""
    rows = []
    for declared in SETTINGS:
        raw = preferences.raw(declared.name)
        source = SET if raw is not None else (DEFAULTED if declared.default is not None else UNSET)
        rows.append((declared, raw if raw is not None else declared.default, source))
    return rows


__all__ = ["BOOTSTRAP", "BY_NAME", "Bounds", "DEFAULTED", "Preferences", "SET", "SETTINGS",
           "Setting",
           "UNSET", "WEIGHTED", "check_environment", "clear_preference", "described",
           "seed_from_env", "set_preference", "setting"]
