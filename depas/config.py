import os
from pathlib import Path

ENV_FILE = Path(".env")


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


def chat_id() -> str:
    """The Telegram chat alerts are posted to."""
    _load_env_file()
    value = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not value:
        raise ValueError("set TELEGRAM_CHAT_ID (run `depas chats` to find it)")
    return value


def optional_text(name: str) -> str | None:
    _load_env_file()
    value = os.environ.get(name, "").strip()
    return value or None
