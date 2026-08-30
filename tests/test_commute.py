import json
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import RequestException

from depas.commute import as_text, estimated_minutes, from_listing, routed_minutes
from depas.config import Location, locations
from depas.grade import Scale

# Estación Central and Los Dominicos: opposite ends of Line 1, an hour apart on foot.
ESTACION_CENTRAL = (-33.45082, -70.67896)
LOS_DOMINICOS = (-33.40789, -70.54499)


@pytest.fixture
def router(monkeypatch):
    """Stand in for Transitous: each call takes the next answer, `None` meaning it is down."""
    answers: list[int | None] = []

    def routed(fetcher, lat, lon, place):
        answer = answers.pop(0)
        if answer is None:
            raise RequestException("transitous is unreachable")
        return answer

    monkeypatch.setattr("depas.commute.routed_minutes", routed)
    return answers


def test_a_location_across_the_street_is_walked():
    """Nothing beats walking 300 m, so the Metro estimate must never win it."""
    assert estimated_minutes(-33.45082, -70.67896, -33.45300, -70.67900) < 5


def test_a_location_across_the_city_takes_the_metro():
    """Walking the length of Line 1 is over two hours; riding it is well under one."""
    assert estimated_minutes(*ESTACION_CENTRAL, *LOS_DOMINICOS) < 45


def test_changing_lines_costs_more_than_staying_on_one():
    """Two points the same distance apart differ by the transfer when no line joins them."""
    on_line_one = estimated_minutes(*ESTACION_CENTRAL, -33.42202, -70.60856)  # Los Leones, L1
    off_line_one = estimated_minutes(*ESTACION_CENTRAL, -33.45419, -70.60497)  # Ñuñoa, L3/L6

    assert off_line_one > on_line_one


def test_any_number_of_locations_can_be_configured(monkeypatch):
    """The list is the whole configuration surface; adding a place is adding an entry."""
    monkeypatch.setenv("DEPAS_LOCATIONS",
                       "a,-33.4172,-70.606; b,-33.4983,-70.6114; c,-33.4408,-70.6506")

    assert [place.name for place in locations()] == ["a", "b", "c"]
    assert (locations()[1].lat, locations()[1].lon) == (-33.4983, -70.6114)


def test_no_locations_configured_measures_nothing():
    """Travel time is opt-in: an unset list must not invent a place to walk to."""
    assert locations() == []
    assert from_listing(None, *ESTACION_CENTRAL) == {}


def test_a_malformed_entry_is_rejected(monkeypatch):
    """A dropped field would shift the others, so a bad entry has to raise."""
    monkeypatch.setenv("DEPAS_LOCATIONS", "a,-33.4172")

    with pytest.raises(ValueError):
        locations()


def test_every_location_is_measured(monkeypatch, router):
    """One listing yields one travel time per configured place."""
    monkeypatch.setenv("DEPAS_LOCATIONS", "a,-33.4172,-70.606; b,-33.4983,-70.6114")
    router.extend([11, 22])

    assert from_listing(None, *ESTACION_CENTRAL) == {"a": 11, "b": 22}


def test_an_unmeasured_listing_renders_as_nothing():
    """A listing with no coordinates has no commute line rather than an empty one."""
    assert as_text(None) == ""
    assert as_text(json.dumps({"a": 32})) == "a 32"


def _pool(*worst: int) -> list[dict]:
    return [{"commute": json.dumps({"a": travel - 3, "b": travel})} for travel in worst]


def test_reaching_everything_inside_the_target_ties_at_the_top(monkeypatch):
    """Under the ideal commute is not a competition — the other axes decide."""
    monkeypatch.setenv("DEPAS_TARGET_COMMUTE", "20")
    monkeypatch.setenv("DEPAS_ALERT_MAX_COMMUTE", "50")
    pool = _pool(8, 15, 20)

    assert len({Scale(pool).grade(row).parts["commute"] for row in pool}) == 1


def test_the_score_falls_between_the_target_and_the_ceiling(monkeypatch):
    """Past the ideal the commute score degrades toward zero at the ceiling."""
    monkeypatch.setenv("DEPAS_TARGET_COMMUTE", "20")
    monkeypatch.setenv("DEPAS_ALERT_MAX_COMMUTE", "50")
    pool = _pool(20, 30, 40, 50)

    scores = [Scale(pool).grade(row).parts["commute"] for row in pool]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_the_worst_location_is_the_one_that_counts(monkeypatch):
    """Both trips have to be made, so a quick hop to one place cannot hide a slow one."""
    monkeypatch.setenv("DEPAS_TARGET_COMMUTE", "20")
    monkeypatch.setenv("DEPAS_ALERT_MAX_COMMUTE", "50")
    near_both = {"commute": json.dumps({"a": 5, "b": 25})}
    near_one = {"commute": json.dumps({"a": 5, "b": 45})}
    scale = Scale([near_both, near_one])

    assert scale.grade(near_both).parts["commute"] > scale.grade(near_one).parts["commute"]


def test_a_routed_answer_beats_the_offline_estimate(monkeypatch, router):
    """Transitous knows the buses; its figure is the one stored."""
    monkeypatch.setenv("DEPAS_LOCATIONS", "a,-33.4104278,-70.5721955")
    router.append(11)

    assert from_listing(None, *ESTACION_CENTRAL) == {"a": 11}


def test_an_unreachable_router_falls_back_to_the_estimate(monkeypatch, router):
    """A best-effort service being down must not stop the hourly pass."""
    monkeypatch.setenv("DEPAS_LOCATIONS", "a,-33.4104278,-70.5721955")
    router.append(None)

    assert from_listing(None, *ESTACION_CENTRAL) == {
        "a": estimated_minutes(*ESTACION_CENTRAL, -33.4104278, -70.5721955)}


def test_the_fastest_trip_wins_whether_it_boards_anything_or_not():
    """`direct` is the walk, `itineraries` board something; the answer is the quicker."""
    plan = {"direct": [{"duration": 1800}], "itineraries": [{"duration": 900}]}
    fetcher = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(json=lambda: plan))

    assert routed_minutes(fetcher, 0.0, 0.0, Location("a", 0.0, 0.0)) == 15


def test_a_route_the_router_cannot_find_is_not_an_answer():
    """No itinerary at all means fall back, not a zero-minute commute."""
    fetcher = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(json=lambda: {"itineraries": []}))

    assert routed_minutes(fetcher, 0.0, 0.0, Location("a", 0.0, 0.0)) is None
