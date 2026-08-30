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
