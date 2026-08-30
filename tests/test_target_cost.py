import pytest

from depas.config import max_rent
from depas.grade import Scale


def _pool(*nets: int) -> list[dict]:
    return [{"net_monthly_clp": net, "area": 50.0, "walk_minutes": 5} for net in nets]


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    monkeypatch.setenv("DEPAS_TARGET_COST", "850000")
    monkeypatch.setenv("DEPAS_ALERT_MAX_COST", "950000")


def test_everything_within_budget_ties_on_cost():
    """Being cheaper than the target is not a competition — only the other axes decide."""
    pool = _pool(500_000, 700_000, 850_000)
    scale = Scale(pool)

    scores = {scale.grade(row).parts["cost"] for row in pool}
    assert len(scores) == 1


def test_the_score_falls_as_a_listing_goes_over_target():
    """Past the target the cost score degrades toward zero at the ceiling."""
    pool = _pool(850_000, 875_000, 900_000, 950_000)
    scale = Scale(pool)

    scores = [scale.grade(row).parts["cost"] for row in pool]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_without_a_target_cost_stays_a_plain_percentile(monkeypatch):
    """Unset the target and cheaper keeps winning outright, as before."""
    monkeypatch.delenv("DEPAS_TARGET_COST")
    pool = _pool(500_000, 700_000, 850_000)
    scale = Scale(pool)

    scores = [scale.grade(row).parts["cost"] for row in pool]
    assert scores[0] > scores[1] > scores[2]


def test_the_rent_ceiling_is_derived_from_the_budget(monkeypatch):
    """Rent above budget plus the most sublet income possible can never come in under."""
    monkeypatch.setenv("DEPAS_PARKING_INCOME", "60000")
    monkeypatch.setenv("DEPAS_STORAGE_INCOME", "30000")

    assert max_rent() == 950_000 + 2 * 60_000 + 30_000


def test_no_budget_means_no_derived_rent_ceiling(monkeypatch):
    """With no budget configured the crawl is not price-bounded at all."""
    monkeypatch.delenv("DEPAS_ALERT_MAX_COST")

    assert max_rent() is None
