"""A grade is measured against the preferences alone, so it never moves when the market does."""
import pytest

from depas.grade import BEST, BREACHED, COMPONENTS, MET, PERFECT_BONUS, Scale
from tests.support import prefs


@pytest.fixture(autouse=True)
def targets(monkeypatch):
    """The numbers the curve is anchored on; a component with no target has no opinion."""
    for name, value in (("DEPAS_COST_TARGET", "800000"), ("DEPAS_COST_MAX", "1000000"),
                        ("DEPAS_WALK_TARGET", "10"), ("DEPAS_WALK_MAX", "15"),
                        ("DEPAS_AREA_TARGET", "50"), ("DEPAS_AREA_MIN", "42")):
        monkeypatch.setenv(name, value)


def _listing(**overrides) -> dict:
    """A listing sitting exactly on every target it is graded against."""
    base = {"price_per_m2_uf_effective": 0.30, "zone_price_per_m2_uf_effective": 0.30,
            "net_monthly_clp": 800_000, "walk_minutes": 10, "area": 50.0, "age": 25,
            "has_elevator": 1, "has_concierge": 1, "has_heating": 1,
            "has_air_conditioning": 1}
    return base | overrides


def test_a_listing_on_every_target_scores_met():
    """Hitting every target is what 100 means, which is the whole point of an absolute grade."""
    graded = Scale(prefs()).grade(_listing())

    assert set(graded.parts.values()) == {MET}
    assert graded.missing == ()


def test_meeting_every_target_on_full_data_earns_the_bonus():
    """The flat that compromises on nothing is worth saying so about, and worth more."""
    graded = Scale(prefs()).grade(_listing())

    assert (graded.meets_targets, graded.missing) == (True, ())
    assert graded.score > MET


def test_the_bonus_needs_the_data_to_back_it_up():
    """Meeting every target on half the axes is a promise, not a proof: the mark, not the +5."""
    scale = Scale(prefs())

    thin = scale.grade(_listing(age=None))

    assert thin.meets_targets is True
    assert thin.score < scale.grade(_listing()).score - PERFECT_BONUS


@pytest.mark.parametrize("field, value, expected", [
    ("net_monthly_clp", 600_000, BEST),
    ("net_monthly_clp", 1_000_000, BREACHED),
    ("net_monthly_clp", 1_200_000, 0),
    ("walk_minutes", 5, BEST),
    ("walk_minutes", 15, BREACHED),
    ("area", 58.0, BEST),
    ("area", 42.0, BREACHED),
])
def test_the_curve_runs_from_best_through_the_target_to_the_limit(field, value, expected):
    """One span better than the target is the top; the hard bound is half; past it, nothing."""
    component = {"net_monthly_clp": "cost", "walk_minutes": "walk", "area": "area"}[field]

    graded = Scale(prefs()).grade(_listing(**{field: value}))

    assert graded.parts[component] == expected


def test_beating_a_target_lifts_the_whole_grade():
    """Passing an expectation must still pay, or a 70 m2 flat ties with the 50 you asked for."""
    scale = Scale(prefs())

    assert scale.grade(_listing(area=58.0)).score > scale.grade(_listing()).score


def test_a_breached_limit_costs_score_and_the_mark():
    """A listing outside a hard bound can still be graded, and has to read as compromised."""
    graded = Scale(prefs()).grade(_listing(net_monthly_clp=1_100_000))

    assert graded.meets_targets is False
    assert graded.parts["cost"] < BREACHED


def test_the_same_listing_always_scores_the_same():
    """Nothing else on the market is consulted, so two readings a month apart agree."""
    assert Scale(prefs()).grade(_listing()).score == 85


def test_missing_components_are_reported_not_guessed():
    """A listing without a benchmark or a walk time is graded on what it has."""
    graded = Scale(prefs()).grade({"net_monthly_clp": 500_000, "area": 60.0})

    assert set(graded.missing) == {"value", "walk", "amenities", "age"}
    assert set(graded.parts) == {"cost", "area"}


def test_partial_data_is_pulled_toward_the_middle():
    """Winning three axes must not beat winning the same three plus three more."""
    scale = Scale(prefs())
    complete = _listing(net_monthly_clp=600_000, walk_minutes=5, area=58.0)

    thin = {key: complete[key] for key in ("net_monthly_clp", "walk_minutes", "area")}

    assert scale.grade(complete).score > scale.grade(thin).score


def test_a_listing_with_no_usable_data_scores_nothing():
    """An empty row must not silently grade mid-table."""
    graded = Scale(prefs()).grade({})

    assert (graded.score, graded.letter) == (0, "?")


def test_weights_shift_the_grade(monkeypatch):
    """Zeroing every weight but cost makes an expensive flat read as badly as its price."""
    for component in COMPONENTS:
        monkeypatch.setenv(f"DEPAS_{component.upper()}_WEIGHT", "0")
    monkeypatch.setenv("DEPAS_COST_WEIGHT", "1")

    assert Scale(prefs()).grade(_listing(net_monthly_clp=1_000_000)).score == BREACHED


def test_all_weights_zero_fails_loudly(monkeypatch):
    """A .env that zeroes everything must raise, not divide by zero."""
    for component in COMPONENTS:
        monkeypatch.setenv(f"DEPAS_{component.upper()}_WEIGHT", "0")

    with pytest.raises(ValueError, match="at least one"):
        Scale(prefs())


def test_a_component_with_no_target_is_off_rather_than_missing(monkeypatch):
    """An unset target is not absent data, it is an opinion you never had."""
    monkeypatch.delenv("DEPAS_COST_TARGET")

    graded = Scale(prefs()).grade(_listing())

    assert "cost" not in graded.parts
    assert "cost" not in graded.missing


def test_security_is_scored_not_filtered(monkeypatch):
    """Wanting 24h security lowers the score of listings without it, never excludes them."""
    monkeypatch.setenv("DEPAS_SECURITY_WANTED", "24 horas")
    scale = Scale(prefs())

    wanted, absent, other = (scale.grade(_listing(security_type=kind))
                             for kind in ("24 horas", None, "conserje diurno"))

    assert wanted.parts["security"] == BEST > other.parts["security"] == BREACHED
    assert "security" in absent.missing


def test_an_unset_security_preference_is_not_a_missing_component(monkeypatch):
    """Without the preference set, no listing should be marked as partially graded for it."""
    monkeypatch.delenv("DEPAS_SECURITY_WANTED", raising=False)

    assert "security" not in Scale(prefs()).grade(_listing()).missing


def test_a_higher_floor_scores_above_a_lower_one(monkeypatch):
    """Height is a preference: below the target costs score, above it earns some."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")
    scale = Scale(prefs())

    high, target, low = (scale.grade(_listing(floor=floor)).parts["floor"]
                         for floor in (10, 5, 1))

    assert high > target == MET > low


def test_the_top_floor_is_docked(monkeypatch):
    """Being the highest floor scores below the identical unit one floor down."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")
    scale = Scale(prefs())

    top = scale.grade(_listing(floor=20, building_floors=20))
    below = scale.grade(_listing(floor=19, building_floors=20))

    assert top.parts["floor"] < below.parts["floor"]


def test_an_unknown_floor_is_missing_not_penalised(monkeypatch):
    """A portal that never publishes a floor must not be graded as if it were floor zero."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")

    graded = Scale(prefs()).grade(_listing(floor=None))

    assert "floor" in graded.missing
    assert graded.letter != "?"


def test_a_listing_that_hides_its_size_cannot_outrank_one_that_states_it():
    """Silence is not free: the unstated metraje is shrunk out rather than assumed good."""
    scale = Scale(prefs())

    unknown = scale.grade(_listing(area=None))

    assert "area" in unknown.missing
    assert unknown.score < scale.grade(_listing(area=50.0)).score


def test_more_amenities_than_expected_still_pay(monkeypatch):
    """Four of nine is the expectation, so a building with eight has to read better."""
    monkeypatch.setenv("DEPAS_AMENITIES_TARGET", "4")
    scale = Scale(prefs())

    extra = _listing(has_pool=1, has_gym=1, has_terrace=1, gated_community=1)

    assert scale.grade(extra).parts["amenities"] > scale.grade(_listing()).parts["amenities"]


def test_amenities_can_be_switched_off(monkeypatch):
    """Expecting none of them turns the component off rather than passing everyone."""
    monkeypatch.setenv("DEPAS_AMENITIES_TARGET", "0")

    graded = Scale(prefs()).grade(_listing())

    assert "amenities" not in graded.parts
    assert "amenities" not in graded.missing


@pytest.mark.parametrize("tiers, stations, ordered", [
    ("1 > 6 > 2", ("Manuel Montt", "Ñuñoa", "Cementerios"), True),
    ("1 > 5", ("Baquedano", "Manuel Montt"), False),
    ("1", ("Manuel Montt", "Ñuñoa"), True),
])
def test_a_station_is_ranked_by_the_best_line_calling_at_it(monkeypatch, tiers, stations, ordered):
    """An interchange takes its better line; an unranked line falls below every ranked one."""
    monkeypatch.setenv("DEPAS_METRO_TIERS", tiers)
    scale = Scale(prefs())

    scores = [scale.grade(_listing(nearest_station=station)).parts["metro"]
              for station in stations]

    assert (scores[0] > scores[-1]) if ordered else (scores[0] == scores[-1])


def test_lines_sharing_a_tier_score_the_same(monkeypatch):
    """Lines 3 and 6 in one tier must tie, and both sit between line 1 and the rest."""
    monkeypatch.setenv("DEPAS_METRO_TIERS", "1 > 3,6 > 2,4,4A,5")
    scale = Scale(prefs())

    one, six, three, two = (scale.grade(_listing(nearest_station=station)).parts["metro"]
                            for station in
                            ("Manuel Montt", "Ñuñoa", "Chile España", "Cementerios"))

    assert one == BEST > six == three == MET > two


def test_no_metro_preference_leaves_the_line_unscored(monkeypatch):
    """Without the setting, the metro line must not become a missing component."""
    monkeypatch.delenv("DEPAS_METRO_TIERS", raising=False)

    assert "metro" not in Scale(prefs()).grade(_listing()).missing


def test_an_older_building_scores_lower_without_being_excluded():
    """Age is a preference: past the target a listing loses score, never the alert."""
    scale = Scale(prefs())

    new, target, older = (scale.grade(_listing(age=age)).parts["age"] for age in (0, 25, 40))

    assert new == BEST > target == MET > older


def test_an_unknown_age_is_missing_not_penalised():
    """Most portals never publish an antigüedad, so its absence must not be graded as old."""
    graded = Scale(prefs()).grade(_listing(age=None))

    assert "age" in graded.missing
    assert graded.letter != "?"


def test_under_25_years_is_the_target_with_nothing_configured(monkeypatch):
    """Age is the one target that stands whether or not DEPAS_AGE_TARGET was ever set."""
    monkeypatch.delenv("DEPAS_AGE_TARGET", raising=False)

    assert Scale(prefs()).grade(_listing(age=25)).parts["age"] == MET
