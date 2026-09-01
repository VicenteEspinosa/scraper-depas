"""Bootstrap: the .env file, and the few constants that are not anybody's preference.

Everything tunable moved to `depas.preferences`, which reads it out of the database.
What is left here is what has to exist before a database can be opened -- where the
environment comes from -- plus the constants that are rules rather than settings.
"""
import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(".env")

# Most publishers simply omit gastos comunes, and treating that as zero makes a
# listing look cheaper than any building it could actually be in. Assume a typical
# Santiago figure instead, and say so wherever the number is shown.
DEFAULT_COMMON_EXPENSES = 120_000

# Under 25 years is the standing rule, so age is the one target that applies whether
# or not anything is configured: clearing DEPAS_TARGET_AGE must not quietly turn the
# preference off. Set it to move the line; zero the weight to ignore it.
DEFAULT_TARGET_AGE = 25

# Everything a comparison cannot fake: what your place costs, how big it is, where it is.
HOME_REQUIRED = ("price_clp", "common_expenses", "area_m2", "lat", "lon")


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


def environment() -> dict[str, str]:
    """The environment with .env folded in, which is what seeds the preferences table."""
    _load_env_file()
    return dict(os.environ)


def secret(name: str) -> str | None:
    """A credential, which stays in the environment rather than moving to the database."""
    _load_env_file()
    return os.environ.get(name, "").strip() or None


def db_path() -> Path:
    """Where the SQLite file lives; bootstrap, so it cannot itself be a preference."""
    _load_env_file()
    return Path(os.environ.get("DEPAS_DB_PATH", "depas.db"))


@dataclass(frozen=True, slots=True)
class Location:
    """A place you have to be able to reach from the apartment."""

    name: str
    lat: float
    lon: float
