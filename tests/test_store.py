from depas.models import Listing
from depas.store import connect, get_setting, save, save_detail, set_setting


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


def test_net_cost_subtracts_lease_income_per_unit(tmp_path):
    """Net cost is rent plus gastos comunes minus what the parking and storage would earn."""
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(600_000)])
    save_detail(connection, "houm", "42",
                {"common_expenses": 100_000, "parking_spaces": 2, "storage_units": 1})

    set_setting(connection, "parking_income", 120_000)
    set_setting(connection, "storage_income", 35_000)

    row = connection.execute("SELECT total_monthly_clp, net_monthly_clp FROM listings_ranked").fetchone()
    assert row["total_monthly_clp"] == 700_000
    assert row["net_monthly_clp"] == 700_000 - 2 * 120_000 - 35_000


def test_lease_income_defaults_to_zero_rather_than_a_guessed_rate(tmp_path):
    """With no income set, net cost equals total cost — no invented market rate."""
    connection = connect(tmp_path / "test.db")
    save(connection, [_listing(600_000)])
    save_detail(connection, "houm", "42", {"common_expenses": 100_000, "parking_spaces": 2})

    assert get_setting(connection, "parking_income") == 0
    row = connection.execute("SELECT total_monthly_clp, net_monthly_clp FROM listings_ranked").fetchone()
    assert row["net_monthly_clp"] == row["total_monthly_clp"] == 700_000
