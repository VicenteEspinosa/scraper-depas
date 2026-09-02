from datetime import UTC, datetime

import pytest

from depas.config import DEFAULT_COMMON_EXPENSES
from depas.models import Listing
from depas.store import MIGRATIONS_DIR, connect, migrate, pool_query, save, save_detail
from tests.support import prefs


def _listing(price: int) -> Listing:
    return Listing(
        portal="houm", external_id="42", url="https://x/42",
        price=price, currency="CLP", area_m2=50.0, price_clp=float(price),
    )


def test_save_tracks_new_then_price_change_then_unchanged(tmp_path):
    """A listing is inserted once, and price_history grows only when the price moves."""
    connection = connect(tmp_path / "test.db")

    first = save(connection, [_listing(500_000)])
    changed = save(connection, [_listing(450_000)])
    repeated = save(connection, [_listing(450_000)])

    assert (first["new"], changed["price_changed"], repeated["unchanged"]) == (1, 1, 1)
    assert connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 2


def test_detail_columns_are_added_to_an_existing_database(tmp_path):
    """Enriching an older database adds columns in place, keeping listings and price history."""
    path = tmp_path / "test.db"
    connection = connect(path)
    save(connection, [_listing(500_000)])
    connection.close()

    reopened = connect(path)
    save_detail(reopened, "houm", "42", {"floor": 7, "has_elevator": 1, "common_expenses": 90_000})

    row = reopened.execute("SELECT * FROM listings").fetchone()
    assert (row["floor"], row["has_elevator"], row["common_expenses"]) == (7, 1, 90_000)
    assert row["price"] == 500_000 and row["detail_fetched_at"] is not None


def test_net_cost_subtracts_lease_income_from_the_environment(tmp_path, monkeypatch):
    """Net cost is rent plus gastos comunes minus what the parking and storage would earn."""
    monkeypatch.setenv("DEPAS_PARKING_INCOME", "60000")
    monkeypatch.setenv("DEPAS_STORAGE_INCOME", "30000")
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(600_000)])

    save_detail(connection, "houm", "42",
                {"common_expenses": 100_000, "parking_spaces": 2, "storage_units": 1})

    row = connection.execute(
        "SELECT total_monthly_clp, net_monthly_clp FROM listings_ranked").fetchone()
    assert row["total_monthly_clp"] == 700_000
    assert row["net_monthly_clp"] == 700_000 - 2 * 60_000 - 30_000


def test_lease_income_defaults_to_zero_rather_than_a_guessed_rate(tmp_path, monkeypatch):
    """With nothing configured, net cost equals total cost — no invented market rate."""
    monkeypatch.delenv("DEPAS_PARKING_INCOME", raising=False)
    monkeypatch.delenv("DEPAS_STORAGE_INCOME", raising=False)
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(600_000)])

    save_detail(connection, "houm", "42", {"common_expenses": 100_000, "parking_spaces": 2})

    row = connection.execute(
        "SELECT total_monthly_clp, net_monthly_clp FROM listings_ranked").fetchone()
    assert row["net_monthly_clp"] == row["total_monthly_clp"] == 700_000


@pytest.mark.parametrize("detail", [{}, {"common_expenses": 0}])
def test_an_undeclared_gasto_comun_falls_back_to_the_default(tmp_path, monkeypatch, detail):
    """Absent or published as zero, gastos comunes cost the assumed default, not nothing."""
    monkeypatch.delenv("DEPAS_PARKING_INCOME", raising=False)
    monkeypatch.delenv("DEPAS_STORAGE_INCOME", raising=False)
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(600_000)])

    save_detail(connection, "houm", "42", {"floor": 7, **detail})

    row = connection.execute(
        "SELECT total_monthly_clp, net_monthly_clp FROM listings_ranked").fetchone()
    assert row["total_monthly_clp"] == row["net_monthly_clp"] == 600_000 + DEFAULT_COMMON_EXPENSES


def test_a_non_numeric_lease_income_fails_loudly(tmp_path, monkeypatch):
    """A typo in .env must raise, not silently fall back to zero income."""
    monkeypatch.setenv("DEPAS_PARKING_INCOME", "60.000 CLP")

    with pytest.raises(ValueError, match="whole number of CLP"):
        connect(tmp_path / "test.db")


def test_every_detail_column_exists_in_the_schema(tmp_path):
    """save_detail writes these names, so a migration that omits one must fail here."""
    from depas.detail import DETAIL_COLUMNS

    connection = connect(tmp_path / "test.db")

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(listings)")}
    assert set(DETAIL_COLUMNS) <= columns


def test_migrations_are_recorded_and_not_reapplied(tmp_path):
    """A second connect applies nothing new — migrations are tracked, not replayed."""
    path = tmp_path / "test.db"
    connect(path).close()

    assert migrate(connect(path)) == []


def test_a_duplicate_env_key_is_rejected(tmp_path, monkeypatch):
    """Two lines for one key silently kept the stale first value — that must raise instead."""
    env_file = tmp_path / ".env"
    env_file.write_text("DEPAS_PARKING_INCOME=1\nDEPAS_PARKING_INCOME=2\n")
    monkeypatch.setattr("depas.config.ENV_FILE", env_file)

    with pytest.raises(ValueError, match="more than once"):
        connect(tmp_path / "test.db")


def test_rescraping_does_not_wipe_detail_data(tmp_path):
    """The hourly watch re-scrapes, so a second save must not blank enriched columns."""
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(500_000)])
    save_detail(connection, "houm", "42",
                {"common_expenses": 90_000, "lat": -33.4, "lon": -70.6, "floor": 7})

    save(connection, [_listing(500_000)])

    row = connection.execute("SELECT * FROM listings").fetchone()
    assert ((row["common_expenses"], row["lat"], row["lon"], row["floor"])
            == (90_000, -33.4, -70.6, 7))


def test_the_backfill_prices_rows_stored_without_one(tmp_path):
    """Rows the bot saved before it converted prices are repaired, UF converted not copied."""
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(600_000)])
    connection.executemany(
        "INSERT INTO listings (portal, external_id, url, price, currency, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, 'now', 'now')",
        [("houm", "43", "https://x/43", 750_000, "CLP"),
         ("houm", "44", "https://x/44", 12.25, "UF")],
    )

    connection.executescript((MIGRATIONS_DIR / "004_backfill_price_clp.sql").read_text())

    priced = dict(connection.execute("SELECT external_id, price_clp FROM listings"))
    assert (priced["42"], priced["43"]) == (600_000, 750_000)
    assert priced["44"] == pytest.approx(500_687.5)


def test_the_ranked_view_prices_per_m2_from_the_cached_uf(tmp_path):
    """The derived UF/m² is silently NULL until a UF is cached, so a pass must store one."""
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(800_000)])

    before = connection.execute(
        "SELECT price_per_m2_uf_effective FROM listings_ranked").fetchone()[0]
    connection.execute("INSERT INTO uf_daily (day, value) VALUES ('2026-01-01', 40000.0)")
    after = connection.execute(
        "SELECT price_per_m2_uf_effective FROM listings_ranked").fetchone()[0]

    assert before is None
    assert after == pytest.approx(0.4)


def test_a_published_year_is_read_as_an_age(tmp_path):
    """Antigüedad comes as years from some portals and as the year built from others."""
    connection = connect(tmp_path / "test.db")
    built = datetime.now(UTC).year - 12
    for external_id, published in (("42", 12), ("43", built), ("44", None)):
        save(connection, [Listing(portal="houm", external_id=external_id,
                                  url=f"https://x/{external_id}", price=500_000,
                                  currency="CLP", price_clp=500_000.0)])
        save_detail(connection, "houm", external_id, {"age_years": published})

    ages = dict(connection.execute("SELECT external_id, age FROM listings_ranked"))

    assert ages == {"42": 12, "43": 12, "44": None}


def test_a_year_still_to_come_is_floored_at_zero(tmp_path):
    """A mistyped year must not make a building younger than new."""
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(500_000)])
    save_detail(connection, "houm", "42", {"age_years": datetime.now(UTC).year + 3})

    assert connection.execute("SELECT age FROM listings_ranked").fetchone()[0] == 0


def test_a_furnished_listing_is_left_out_of_the_pool(tmp_path):
    """Amoblado is excluded outright, and a listing nobody would take must not set the curve."""
    connection = connect(tmp_path / "test.db")
    for external_id, title in (("42", "Depto 2D"), ("43", "Depto 2D"), ("44", "Depto AMOBLADO 2D")):
        save(connection, [Listing(portal="houm", external_id=external_id, title=title,
                                  url=f"https://x/{external_id}", price=500_000,
                                  currency="CLP", price_clp=500_000.0)])
        save_detail(connection, "houm", external_id,
                    {"furnished": 1 if external_id == "43" else None})

    pooled = [row["external_id"] for row in connection.execute(pool_query(prefs()))]

    assert pooled == ["42"]  # 43 says so in its spec table, 44 only in its title
