import pytest

from depas.grade import Scale


def _listing(**overrides) -> dict:
    base = {"price_per_m2_uf": 0.30, "zone_price_per_m2_uf": 0.37, "net_monthly_clp": 700_000,
            "walk_minutes": 8, "area": 50.0, "has_elevator": 1}
    return base | overrides


def _pool(count: int = 21) -> list[dict]:
    # step 0 is the best listing on every axis, step -1 the worst
    return [_listing(net_monthly_clp=400_000 + step * 30_000, walk_minutes=step, area=80.0 - step)
            for step in range(count)]


def test_score_is_a_percentile_against_the_pool():
    """The cheapest, closest, largest listing outranks the pool; the worst sits at the bottom."""
    pool = _pool()
    scale = Scale(pool)

    best, middle, worst = (scale.grade(pool[i]).score for i in (0, len(pool) // 2, -1))

    assert best > middle > worst
    assert (best, middle, worst) == (98, 50, 2)


def test_grades_spread_across_letters():
    """A pool graded against itself uses the whole range, not one clustered letter."""
    pool = _pool(40)
    scale = Scale(pool)

    letters = {scale.grade(row).letter for row in pool}
    assert {"A", "B", "C", "D", "E"} <= letters


def test_missing_components_are_reported_not_guessed():
    """A listing without coordinates or a benchmark is graded on what it has."""
    scale = Scale(_pool())

    graded = scale.grade({"net_monthly_clp": 500_000, "area": 60.0})

    assert set(graded.missing) == {"value", "location", "amenities"}
    assert set(graded.parts) == {"cost", "size"}


def test_a_listing_with_no_usable_data_scores_nothing():
    """An empty row must not silently rank mid-table."""
    graded = Scale(_pool()).grade({})

    assert (graded.score, graded.letter) == (0, "?")


def test_weights_shift_the_ranking(monkeypatch):
    """Zeroing every weight but location makes the closest listing win outright."""
    pool = _pool()
    far_but_cheap = _listing(net_monthly_clp=100_000, walk_minutes=30, area=99.0)

    monkeypatch.setenv("DEPAS_WEIGHT_VALUE", "0")
    monkeypatch.setenv("DEPAS_WEIGHT_COST", "0")
    monkeypatch.setenv("DEPAS_WEIGHT_SIZE", "0")
    monkeypatch.setenv("DEPAS_WEIGHT_AMENITIES", "0")

    assert Scale(pool).grade(far_but_cheap).score == 0


def test_all_weights_zero_fails_loudly(monkeypatch):
    """A .env that zeroes everything must raise, not divide by zero."""
    for component in ("VALUE", "COST", "LOCATION", "SIZE", "AMENITIES"):
        monkeypatch.setenv(f"DEPAS_WEIGHT_{component}", "0")

    with pytest.raises(ValueError, match="at least one"):
        Scale(_pool())
