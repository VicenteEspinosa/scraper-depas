"""A trait is a yes/no property you either will not take, merely dislike, or ignore."""
import pytest

from depas.grade import Scale
from depas.models import Listing
from depas.preferences import Preferences
from depas.store import connect, pool_query, save, save_detail, store_preference


def _save(connection, external_id, **detail):
    save(connection, [Listing(portal="houm", external_id=external_id, title="Depto 2D",
                              url=f"https://x/{external_id}", price=500_000,
                              currency="CLP", price_clp=500_000.0, area_m2=50.0)])
    save_detail(connection, "houm", external_id, {"bedrooms": 2, **detail})


@pytest.fixture
def pool(tmp_path):
    """Three identical listings but for the trait each one carries."""
    connection = connect(tmp_path / "test.db")
    # A penalty is docked inside a component, so the component has to be switched on.
    store_preference(connection, "DEPAS_FLOOR_TARGET", "5")
    _save(connection, "plain", floor=3, building_floors=10)
    _save(connection, "furnished", floor=3, building_floors=10, furnished=1)
    _save(connection, "top", floor=10, building_floors=10)
    return connection


def _pooled(connection):
    prefs = Preferences.load(connection)
    return sorted(row["external_id"] for row in connection.execute(pool_query(prefs)))


def _scores(connection):
    prefs = Preferences.load(connection)
    scale = Scale(prefs)
    return {row["external_id"]: scale.grade(dict(row)).score
            for row in connection.execute(pool_query(prefs))}


@pytest.mark.parametrize("disposition, expected", [
    ("exclude", ["plain", "top"]),
    ("penalise", ["furnished", "plain", "top"]),
    ("ignore", ["furnished", "plain", "top"]),
])
def test_only_excluding_takes_a_listing_out_of_the_pool(pool, disposition, expected):
    """Penalising costs score; excluding also stops the listing setting the curve."""
    store_preference(pool, "DEPAS_FURNISHED", disposition)

    assert _pooled(pool) == expected


def test_a_penalised_trait_scores_below_an_otherwise_identical_listing(pool):
    """Kept in the pool, but ranked under the same flat without the trait."""
    store_preference(pool, "DEPAS_FURNISHED", "penalise")

    scores = _scores(pool)

    assert scores["furnished"] < scores["plain"]


def test_an_ignored_trait_costs_nothing(pool):
    """Ignoring means the trait stops being read, not that it is read and forgiven."""
    store_preference(pool, "DEPAS_FURNISHED", "ignore")

    scores = _scores(pool)

    assert scores["furnished"] == scores["plain"]


def test_a_listing_that_answers_nothing_is_not_scored_on_traits(pool):
    """Silence is not the same as not having the trait, so it must not earn a free pass."""
    store_preference(pool, "DEPAS_FURNISHED", "penalise")

    graded = Scale(Preferences.load(pool)).grade({"net_monthly_clp": 500_000})

    assert "traits" not in graded.parts


def test_the_top_floor_penalty_stays_inside_the_floor_component(pool):
    """It competes against the height the flat already earned, where its size means something."""
    prefs = Preferences.load(pool)
    scale = Scale(prefs)
    top = next(dict(row) for row in pool.execute(pool_query(prefs))
               if row["external_id"] == "top")

    graded = scale.grade(top)

    assert "traits" not in graded.parts
    assert graded.parts["floor"] < scale.grade(top | {"building_floors": 11}).parts["floor"]


def test_an_unknown_disposition_is_refused(pool):
    """A trait takes three words, and anything else is somebody's typo."""
    with pytest.raises(ValueError, match="must be one of"):
        store_preference(pool, "DEPAS_FURNISHED", "maybe")
