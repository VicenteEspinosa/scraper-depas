"""Bootstrap: the .env file, and the few constants that are not anybody's preference."""
import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(".env")
# The starting set a fresh database is seeded from; .env overrides it, then neither is read.
SEED_FILE = Path("seed.env")

# Most publishers omit gastos comunes, and zero would make a listing look impossibly cheap.
DEFAULT_COMMON_EXPENSES = 120_000

# The one target that applies unconfigured: clearing DEPAS_AGE_TARGET must not turn it off.
DEFAULT_TARGET_AGE = 25

# Everything a comparison cannot fake: what your place costs, how big it is, where it is.
HOME_REQUIRED = ("price_clp", "common_expenses", "area_m2", "lat", "lon")


def _parse(path: Path) -> dict[str, str]:
    """`KEY=value` lines, comments and blanks skipped, in file order."""
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        # A repeated key silently kept the first value, which hides a pasted-in update.
        if key in found:
            raise ValueError(f"{path} defines {key} more than once; keep a single line")
        found[key] = value.strip()
    return found


def _load_env_file() -> None:
    """Read .env into the environment without overriding anything already exported."""
    if not ENV_FILE.exists():
        return
    for key, value in _parse(ENV_FILE).items():
        os.environ.setdefault(key, value)


def defaults() -> dict[str, str]:
    """The checked-in starting set, which anything the environment says overrides."""
    return _parse(SEED_FILE) if SEED_FILE.exists() else {}


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
