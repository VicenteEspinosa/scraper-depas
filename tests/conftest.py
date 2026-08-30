import os
from pathlib import Path

from pytest import MonkeyPatch, fixture


@fixture(autouse=True)
def isolated_config(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Keep the suite hermetic: a developer's own .env or exports must not change outcomes."""
    monkeypatch.setattr("depas.config.ENV_FILE", tmp_path / "absent.env")
    # Every setting, not a list that has to be remembered: a new DEPAS_* would otherwise
    # leak in from the shell and make a local run disagree with CI.
    for name in [name for name in os.environ if name.startswith(("DEPAS_", "TELEGRAM_"))]:
        monkeypatch.delenv(name, raising=False)
