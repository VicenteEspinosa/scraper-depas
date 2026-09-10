import os
from pathlib import Path

from pytest import MonkeyPatch, fixture

from depas import telegram


@fixture(autouse=True)
def isolated_config(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Keep the suite hermetic: a developer's own .env or exports must not change outcomes."""
    monkeypatch.setattr("depas.config.ENV_FILE", tmp_path / "absent.env")
    # seed.env sits in the repo root, so without this every test would be seeded with
    # the checked-in configuration and quietly depend on whatever it happens to say.
    monkeypatch.setattr("depas.config.SEED_FILE", tmp_path / "absent-seed.env")
    # Every setting, not a list that has to be remembered: a new DEPAS_* would otherwise
    # leak in from the shell and make a local run disagree with CI.
    for name in [name for name in os.environ if name.startswith(("DEPAS_", "TELEGRAM_"))]:
        monkeypatch.delenv(name, raising=False)


@fixture(autouse=True)
def unpaced(monkeypatch: MonkeyPatch) -> None:
    """No test may wait out a Telegram rate limit: the pacing is real, the waiting is not.

    The interval goes to zero rather than `time.sleep` being stubbed, so a test that
    wants to assert on a wait — the 429 retry does — still has a real one to assert on.
    """
    monkeypatch.setattr(telegram, "CROWDED_PACE_SECONDS", 0)
    monkeypatch.setattr(telegram, "PRIVATE_PACE_SECONDS", 0)
    # Per process, so what one test posted must not make the next one wait.
    telegram._LAST_SENT.clear()
