import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(".env")

# Most publishers simply omit gastos comunes, and treating that as zero makes a
# listing look cheaper than any building it could actually be in. Assume a typical
# Santiago figure instead, and say so wherever the number is shown.
DEFAULT_COMMON_EXPENSES = 120_000


def _load_env_file() -> None:
    """Read .env into the environment without overriding anything already exported."""
    if not ENV_FILE.exists():
        return
    seen: set[str] = set()
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        # A repeated key silently kept the first value, which hides a pasted-in update.
        if key in seen:
            raise ValueError(f"{ENV_FILE} defines {key} more than once; keep a single line")
        seen.add(key)
        os.environ.setdefault(key, value.strip())


def lease_income(kind: str) -> int:
    """Monthly CLP a parking space or storage unit is expected to earn, from the environment."""
    _load_env_file()
    raw = os.environ.get(f"DEPAS_{kind.upper()}_INCOME", "0")
    if not raw.isdigit():
        raise ValueError(f"DEPAS_{kind.upper()}_INCOME must be a whole number of CLP, got {raw!r}")
    return int(raw)


def alert_communes() -> list[str]:
    """Communes the hourly watch scrapes, as portal slugs."""
    _load_env_file()
    raw = os.environ.get("DEPAS_ALERT_COMMUNES", "")
    return [slug.strip() for slug in raw.split(",") if slug.strip()]


def optional_int(name: str) -> int | None:
    _load_env_file()
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    if not raw.lstrip("-").isdigit():
        raise ValueError(f"{name} must be a whole number, got {raw!r}")
    return int(raw)


def line_preference() -> list[list[str]]:
    """Metro lines in tiers, best first; lines sharing a tier are worth the same."""
    _load_env_file()
    raw = os.environ.get("DEPAS_LINE_PREFERENCE", "")
    tiers = [[line.strip().upper() for line in tier.split(",") if line.strip()]
             for tier in raw.split(">")]
    return [tier for tier in tiers if tier]


@dataclass(frozen=True, slots=True)
class Location:
    """A place you have to be able to reach from the apartment."""

    name: str
    lat: float
    lon: float


def locations() -> list[Location]:
    """DEPAS_LOCATIONS as `name,lat,lon` entries separated by `;`."""
    _load_env_file()
    found = []
    for entry in os.environ.get("DEPAS_LOCATIONS", "").split(";"):
        if not entry.strip():
            continue
        parts = [part.strip() for part in entry.split(",")]
        if len(parts) != 3:
            raise ValueError(f"DEPAS_LOCATIONS entry must be name,lat,lon: {entry!r}")
        name, lat, lon = parts
        found.append(Location(name, float(lat), float(lon)))
    return found


def chat_id() -> str:
    """The Telegram chat alerts are posted to: a channel for commentable cards, or any group."""
    _load_env_file()
    value = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not value:
        raise ValueError("set TELEGRAM_CHAT_ID (run `depas chats` to find it)")
    return value


def optional_text(name: str) -> str | None:
    _load_env_file()
    value = os.environ.get(name, "").strip()
    return value or None


HOME_VAR = "DEPAS_CURRENT_HOME"
# Everything a comparison cannot fake: what your place costs, how big it is, where it is.
HOME_REQUIRED = ("price_clp", "common_expenses", "area_m2", "lat", "lon")


def current_home() -> dict | None:
    """Your own apartment as one JSON object, or None when you have not described it."""
    _load_env_file()
    raw = os.environ.get(HOME_VAR, "").strip()
    if not raw:
        return None
    try:
        home = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{HOME_VAR} must be a single JSON object: {error}") from None
    missing = [name for name in HOME_REQUIRED if home.get(name) is None]
    if missing:
        raise ValueError(f"{HOME_VAR} is missing {', '.join(missing)}")
    return home


def home_net_monthly_clp(home: dict) -> int:
    """What your place costs a month, priced on the same terms as a listing."""
    return round(home["price_clp"] + home["common_expenses"]
                 - (home.get("parking_spaces") or 0) * lease_income("parking")
                 - (home.get("storage_units") or 0) * lease_income("storage"))


def current_cost() -> int | None:
    """What you pay now, net, so every listing can be shown as a difference."""
    configured = optional_int("DEPAS_CURRENT_COST")
    if configured is not None:
        return configured
    home = current_home()
    return None if home is None else home_net_monthly_clp(home)


def target_cost() -> int | None:
    """What we aim to spend; listings above it score worse without being excluded."""
    return optional_int("DEPAS_TARGET_COST")


# Under 25 years is the standing rule, so age is the one target that applies whether
# or not anything is configured: leaving DEPAS_TARGET_AGE unset must not quietly turn
# the preference off. Set the variable to move the line; zero the weight to ignore it.
DEFAULT_TARGET_AGE = 25


def target_age() -> int:
    """The antigüedad we aim to stay under; older listings score worse, never excluded."""
    configured = optional_int("DEPAS_TARGET_AGE")
    return DEFAULT_TARGET_AGE if configured is None else configured


def max_rent() -> int | None:
    """Rent ceiling for the crawl, derived from the budget rather than configured.

    Gastos comunes only add to the net cost and sublet income is the only thing that
    subtracts, so rent above budget-plus-maximum-sublet can never come in under budget.
    """
    budget = optional_int("DEPAS_ALERT_MAX_COST")
    if budget is None:
        return None
    return budget + 2 * lease_income("parking") + lease_income("storage")
