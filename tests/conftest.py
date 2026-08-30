from pathlib import Path

from pytest import MonkeyPatch, fixture


@fixture(autouse=True)
def isolated_config(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Keep the suite hermetic: a developer's own .env must not change test outcomes."""
    monkeypatch.setattr("depas.config.ENV_FILE", tmp_path / "absent.env")
    for name in ("DEPAS_PARKING_INCOME", "DEPAS_STORAGE_INCOME",
                 "DEPAS_ALERT_SECURITY", "DEPAS_TARGET_WALK", "DEPAS_ALERT_MAX_WALK",
                 *(f"DEPAS_WEIGHT_{c}" for c in
                   ("VALUE", "COST", "LOCATION", "SIZE", "AMENITIES", "SECURITY"))):
        monkeypatch.delenv(name, raising=False)
