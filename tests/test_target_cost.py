import pytest

from depas.grade import Scale
from tests.support import prefs


def _pool(*nets: int) -> list[dict]:
    return [{"net_monthly_clp": net, "area": 50.0, "walk_minutes": 5} for net in nets]


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    monkeypatch.setenv("DEPAS_COST_MAX", "950000")


def test_everything_within_budget_ties_on_cost():
    """Being cheaper than the target is not a competition — only the other axes decide."""
    pool = _pool(500_000, 700_000, 850_000)
    scale = Scale(pool, prefs())

    scores = {scale.grade(row).parts["cost"] for row in pool}
    assert len(scores) == 1


def test_the_score_falls_as_a_listing_goes_over_target():
    """Past the target the cost score degrades toward zero at the ceiling."""
    pool = _pool(850_000, 875_000, 900_000, 950_000)
    scale = Scale(pool, prefs())

    scores = [scale.grade(row).parts["cost"] for row in pool]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_without_a_target_cost_stays_a_plain_percentile(monkeypatch):
    """Unset the target and cheaper keeps winning outright, as before."""
    monkeypatch.delenv("DEPAS_COST_TARGET")
    pool = _pool(500_000, 700_000, 850_000)
    scale = Scale(pool, prefs())

    scores = [scale.grade(row).parts["cost"] for row in pool]
    assert scores[0] > scores[1] > scores[2]


def test_the_rent_ceiling_is_derived_from_the_budget(monkeypatch):
    """Rent above budget plus the most sublet income possible can never come in under."""
    monkeypatch.setenv("DEPAS_PARKING_INCOME", "60000")
    monkeypatch.setenv("DEPAS_STORAGE_INCOME", "30000")

    assert prefs().max_rent() == 950_000 + 2 * 60_000 + 30_000


def test_no_budget_means_no_derived_rent_ceiling(monkeypatch):
    """With no budget configured the crawl is not price-bounded at all."""
    monkeypatch.delenv("DEPAS_COST_MAX")

    assert prefs().max_rent() is None


def test_walking_within_the_ideal_ties_at_the_top(monkeypatch):
    """Two minutes and ten minutes are both fine, so neither should outrank the other."""
    monkeypatch.setenv("DEPAS_WALK_TARGET", "10")
    monkeypatch.setenv("DEPAS_WALK_MAX", "15")
    pool = [{"walk_minutes": w, "net_monthly_clp": 700_000, "area": 50.0} for w in (2, 6, 10)]

    scores = {Scale(pool, prefs()).grade(row).parts["walk"] for row in pool}

    assert len(scores) == 1


def test_walking_past_the_ideal_costs_score_without_excluding(monkeypatch):
    """Between the ideal and the ceiling the score falls, but the listing still ranks."""
    monkeypatch.setenv("DEPAS_WALK_TARGET", "10")
    monkeypatch.setenv("DEPAS_WALK_MAX", "15")
    pool = [{"walk_minutes": w, "net_monthly_clp": 700_000, "area": 50.0} for w in (10, 12, 14, 15)]
    scale = Scale(pool, prefs())

    scores = [scale.grade(row).parts["walk"] for row in pool]

    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1] > 0
