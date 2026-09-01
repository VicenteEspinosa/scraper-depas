"""What a target and a hard bound do to a number: the target is the anchor, the bound the span."""
import pytest

from depas.grade import BEST, BREACHED, MET, Scale
from tests.support import prefs


def _listing(**overrides) -> dict:
    return {"net_monthly_clp": 850_000, "area": 50.0, "walk_minutes": 5} | overrides


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    monkeypatch.setenv("DEPAS_COST_MAX", "950000")


def test_coming_in_under_the_target_keeps_paying():
    """Being cheaper than you asked for is worth score, not a tie at the target."""
    scale = Scale(prefs())

    at_target, under, well_under = (scale.grade(_listing(net_monthly_clp=net)).parts["cost"]
                                    for net in (850_000, 800_000, 500_000))

    assert at_target == MET < under < well_under == BEST


def test_the_score_falls_as_a_listing_goes_over_target():
    """Past the target the cost score degrades, reaching half at the ceiling."""
    scale = Scale(prefs())

    scores = [scale.grade(_listing(net_monthly_clp=net)).parts["cost"]
              for net in (850_000, 875_000, 900_000, 950_000)]

    assert scores == sorted(scores, reverse=True)
    assert (scores[0], scores[-1]) == (MET, BREACHED)


def test_without_a_target_there_is_nothing_to_score_cost_against(monkeypatch):
    """A ceiling alone says what you refuse, not what you want, so the component is off."""
    monkeypatch.delenv("DEPAS_COST_TARGET")

    graded = Scale(prefs()).grade(_listing())

    assert "cost" not in graded.parts


def test_the_ceiling_sets_how_fast_the_score_falls(monkeypatch):
    """A tighter ceiling makes the same overspend cost more, because the span is shorter."""
    over_budget = _listing(net_monthly_clp=900_000)
    tight = Scale(prefs()).grade(over_budget).parts["cost"]

    monkeypatch.setenv("DEPAS_COST_MAX", "1050000")

    assert Scale(prefs()).grade(over_budget).parts["cost"] > tight


def test_the_rent_ceiling_is_derived_from_the_budget(monkeypatch):
    """Rent above budget plus the most sublet income possible can never come in under."""
    monkeypatch.setenv("DEPAS_PARKING_INCOME", "60000")
    monkeypatch.setenv("DEPAS_STORAGE_INCOME", "30000")

    assert prefs().max_rent() == 950_000 + 2 * 60_000 + 30_000


def test_no_budget_means_no_derived_rent_ceiling(monkeypatch):
    """With no budget configured the crawl is not price-bounded at all."""
    monkeypatch.delenv("DEPAS_COST_MAX")

    assert prefs().max_rent() is None


def test_walking_less_than_the_ideal_earns_score(monkeypatch):
    """Two minutes from the metro beats the ten you said you would accept."""
    monkeypatch.setenv("DEPAS_WALK_TARGET", "10")
    monkeypatch.setenv("DEPAS_WALK_MAX", "15")
    scale = Scale(prefs())

    close, ideal = (scale.grade(_listing(walk_minutes=walk)).parts["walk"] for walk in (5, 10))

    assert close == BEST > ideal == MET


def test_walking_past_the_ideal_costs_score_without_excluding(monkeypatch):
    """Between the ideal and the ceiling the score falls, but the listing still grades."""
    monkeypatch.setenv("DEPAS_WALK_TARGET", "10")
    monkeypatch.setenv("DEPAS_WALK_MAX", "15")
    scale = Scale(prefs())

    scores = [scale.grade(_listing(walk_minutes=walk)).parts["walk"] for walk in (10, 12, 14, 15)]

    assert scores == sorted(scores, reverse=True)
    assert scores[-1] == BREACHED
