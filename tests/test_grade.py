import pytest

from depas.grade import COMPONENTS, Scale
from tests.support import prefs


def _listing(**overrides) -> dict:
    base = {"price_per_m2_uf_effective": 0.30, "zone_price_per_m2_uf_effective": 0.37,
            "net_monthly_clp": 700_000,
            "walk_minutes": 8, "area": 50.0, "has_elevator": 1}
    return base | overrides


def _pool(count: int = 21) -> list[dict]:
    # step 0 is the best listing on every axis, step -1 the worst
    return [_listing(net_monthly_clp=400_000 + step * 30_000, walk_minutes=step, area=80.0 - step)
            for step in range(count)]


def test_score_is_a_percentile_against_the_pool():
    """The cheapest, closest, largest listing outranks the pool; the worst sits at the bottom."""
    pool = _pool()
    scale = Scale(pool, prefs())

    best, middle, worst = (scale.grade(pool[i]).score for i in (0, len(pool) // 2, -1))

    assert best > middle > worst
    assert (best, middle, worst) == (98, 50, 2)


def test_grades_spread_across_letters():
    """A pool graded against itself uses the whole range, not one clustered letter."""
    pool = _pool(40)
    scale = Scale(pool, prefs())

    letters = {scale.grade(row).letter for row in pool}
    assert {"A", "B", "C", "D", "E"} <= letters


def test_missing_components_are_reported_not_guessed():
    """A listing without coordinates or a benchmark is graded on what it has."""
    scale = Scale(_pool(), prefs())

    graded = scale.grade({"net_monthly_clp": 500_000, "area": 60.0})

    assert set(graded.missing) == {"value", "walk", "amenities"}
    assert set(graded.parts) == {"cost", "area"}


def test_a_listing_with_no_usable_data_scores_nothing():
    """An empty row must not silently rank mid-table."""
    graded = Scale(_pool(), prefs()).grade({})

    assert (graded.score, graded.letter) == (0, "?")


def test_weights_shift_the_ranking(monkeypatch):
    """Zeroing every weight but location makes the closest listing win outright."""
    pool = _pool()
    far_but_cheap = _listing(net_monthly_clp=100_000, walk_minutes=30, area=99.0)

    monkeypatch.setenv("DEPAS_VALUE_WEIGHT", "0")
    monkeypatch.setenv("DEPAS_COST_WEIGHT", "0")
    monkeypatch.setenv("DEPAS_AREA_WEIGHT", "0")
    monkeypatch.setenv("DEPAS_AMENITIES_WEIGHT", "0")

    assert Scale(pool, prefs()).grade(far_but_cheap).score == 0


def test_all_weights_zero_fails_loudly(monkeypatch):
    """A .env that zeroes everything must raise, not divide by zero."""
    for component in COMPONENTS:
        monkeypatch.setenv(f"DEPAS_{component.upper()}_WEIGHT", "0")

    with pytest.raises(ValueError, match="at least one"):
        Scale(_pool(), prefs())


def test_security_is_scored_not_filtered(monkeypatch):
    """Wanting 24h security lowers the score of listings without it, never excludes them."""
    monkeypatch.setenv("DEPAS_SECURITY_WANTED", "24 horas")
    pool = [{"security_type": s, "net_monthly_clp": 700_000, "area": 50.0, "walk_minutes": 5}
            for s in ("24 horas", None, "conserje diurno")]
    scale = Scale(pool, prefs())

    wanted, absent, other = (scale.grade(row) for row in pool)

    assert wanted.score > absent.score
    assert absent.score == other.score
    assert all(g.letter != "?" for g in (wanted, absent, other))


def test_an_unset_security_preference_is_not_a_missing_component(monkeypatch):
    """Without the preference set, no listing should be marked as partially graded for it."""
    monkeypatch.delenv("DEPAS_SECURITY_WANTED", raising=False)
    pool = [{"net_monthly_clp": 700_000, "area": 50.0, "walk_minutes": 5, "has_elevator": 1}]

    assert "security" not in Scale(pool, prefs()).grade(pool[0]).missing


def test_floor_below_target_scores_lower_without_being_excluded(monkeypatch):
    """A low floor costs score, so listings on it still alert rather than disappearing."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")
    pool = [_listing(floor=floor, building_floors=20) for floor in (8, 5, 3, 1)]
    scale = Scale(pool, prefs())

    high, target, low, ground = (scale.grade(row).score for row in pool)

    assert high == target > low > ground
    assert all(scale.grade(row).letter != "?" for row in pool)


def test_the_top_floor_is_docked(monkeypatch):
    """Being the highest floor scores below the identical unit one floor down."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")
    pool = [_listing(floor=20, building_floors=20), _listing(floor=19, building_floors=20)]
    scale = Scale(pool, prefs())

    top, below = (scale.grade(row).score for row in pool)

    assert top < below


def test_an_unknown_floor_is_missing_not_penalised(monkeypatch):
    """A portal that never publishes a floor must not be graded as if it were floor zero."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")
    pool = [_listing(floor=5, building_floors=20), _listing(floor=None)]
    scale = Scale(pool, prefs())

    assert "floor" in scale.grade(pool[1]).missing
    assert scale.grade(pool[1]).letter != "?"


def test_area_at_the_target_ties_at_the_top(monkeypatch):
    """Past the ideal size, extra square metres stop earning score."""
    monkeypatch.setenv("DEPAS_AREA_TARGET", "50")
    monkeypatch.setenv("DEPAS_AREA_MIN", "42")
    pool = [_listing(area=a) for a in (80.0, 50.0, 46.0, 42.0)]
    scale = Scale(pool, prefs())

    huge, target, middling, smallest = (scale.grade(row).score for row in pool)

    assert huge == target > middling > smallest


def test_an_unknown_area_scores_bottom_but_still_grades(monkeypatch):
    """A listing that hides its size must not outrank one that states it, nor be dropped."""
    monkeypatch.setenv("DEPAS_AREA_TARGET", "50")
    monkeypatch.setenv("DEPAS_AREA_MIN", "42")
    pool = [_listing(area=50.0), _listing(area=42.0), _listing(area=None)]
    scale = Scale(pool, prefs())

    stated, smallest, unknown = (scale.grade(row) for row in pool)

    assert stated.score > unknown.score
    assert unknown.score == smallest.score
    assert "size" not in unknown.missing


def test_a_thin_grade_cannot_outrank_a_complete_one(monkeypatch):
    """Winning four axes must not beat winning the same four plus three more."""
    monkeypatch.setenv("DEPAS_FLOOR_TARGET", "5")
    monkeypatch.setenv("DEPAS_AREA_TARGET", "50")
    monkeypatch.setenv("DEPAS_SECURITY_WANTED", "24 horas")
    shared = {"net_monthly_clp": 500_000, "walk_minutes": 2, "area": 90.0,
              "security_type": "24 horas"}
    complete = shared | {"price_per_m2_uf": 0.2, "zone_price_per_m2_uf": 0.5,
                         "has_elevator": 1, "floor": 8, "building_floors": 20}
    thin = dict(shared)
    filler = [{"net_monthly_clp": 900_000, "walk_minutes": 14, "area": 42.0,
               "price_per_m2_uf": 0.6, "zone_price_per_m2_uf": 0.5, "has_pool": 0,
               "floor": 1, "building_floors": 1, "security_type": None}]
    scale = Scale([complete, thin] + filler, prefs())

    assert scale.grade(complete).score > scale.grade(thin).score


def test_coverage_only_pulls_toward_the_middle(monkeypatch):
    """Shrinking must never flip a thin listing below a genuinely worse complete one."""
    monkeypatch.setenv("DEPAS_AREA_TARGET", "50")
    good_thin = {"net_monthly_clp": 500_000, "area": 90.0}
    bad_complete = {"net_monthly_clp": 950_000, "area": 42.0, "walk_minutes": 15,
                    "price_per_m2_uf": 0.9, "zone_price_per_m2_uf": 0.5, "has_pool": 0}
    scale = Scale([good_thin, bad_complete], prefs())

    assert scale.grade(good_thin).score > scale.grade(bad_complete).score


def test_a_preferred_line_scores_above_a_less_preferred_one(monkeypatch):
    """Ranking line 1 first must lift its stations above stations on lines ranked later."""
    monkeypatch.setenv("DEPAS_METRO_TIERS", "1 > 6 > 2")
    pool = [_listing(nearest_station=station)
            for station in ("Manuel Montt", "Ñuñoa", "Cementerios")]
    scale = Scale(pool, prefs())

    line_one, line_six, line_two = (scale.grade(row).score for row in pool)

    assert line_one > line_six > line_two


def test_an_interchange_is_judged_on_its_better_line(monkeypatch):
    """Baquedano is on 1 and 5; ranking 1 first must score it as a line 1 station."""
    monkeypatch.setenv("DEPAS_METRO_TIERS", "1 > 5")
    pool = [_listing(nearest_station="Baquedano"), _listing(nearest_station="Manuel Montt"),
            _listing(nearest_station="Bellavista de La Florida")]
    scale = Scale(pool, prefs())

    interchange, only_one, only_five = (scale.grade(row).score for row in pool)

    assert interchange == only_one > only_five


def test_an_unranked_line_falls_below_every_ranked_one(monkeypatch):
    """Naming only line 1 must not make every other line tie with it."""
    monkeypatch.setenv("DEPAS_METRO_TIERS", "1")
    pool = [_listing(nearest_station="Manuel Montt"), _listing(nearest_station="Ñuñoa")]
    scale = Scale(pool, prefs())

    assert scale.grade(pool[0]).score > scale.grade(pool[1]).score


def test_no_preference_leaves_the_line_unscored(monkeypatch):
    """Without the setting, the metro line must not become a missing component."""
    monkeypatch.delenv("DEPAS_METRO_TIERS", raising=False)
    pool = [_listing(nearest_station="Manuel Montt")]

    assert "metro" not in Scale(pool, prefs()).grade(pool[0]).missing


def test_lines_sharing_a_tier_score_the_same(monkeypatch):
    """Lines 3 and 6 in one tier must tie, and both sit between line 1 and the rest."""
    monkeypatch.setenv("DEPAS_METRO_TIERS", "1 > 3,6 > 2,4,4A,5")
    pool = [_listing(nearest_station=station) for station in
            ("Manuel Montt", "Ñuñoa", "Chile España", "Cementerios")]
    scale = Scale(pool, prefs())

    line_one, line_six, line_three, line_two = (scale.grade(row).score for row in pool)

    assert line_one > line_six == line_three > line_two


def test_an_older_building_scores_lower_without_being_excluded(monkeypatch):
    """Age is a preference: past the target a listing loses score, never the alert."""
    monkeypatch.setenv("DEPAS_AGE_TARGET", "25")
    pool = [_listing(age=age) for age in (0, 25, 40, 60)]
    scale = Scale(pool, prefs())

    new, target, older, oldest = (scale.grade(row).score for row in pool)

    assert new == target > older > oldest
    assert all(scale.grade(row).letter != "?" for row in pool)


def test_an_unknown_age_is_missing_not_penalised(monkeypatch):
    """Most portals never publish an antigüedad, so its absence must not be graded as old."""
    monkeypatch.setenv("DEPAS_AGE_TARGET", "25")
    pool = [_listing(age=5), _listing(age=None)]
    scale = Scale(pool, prefs())

    assert "age" in scale.grade(pool[1]).missing
    assert scale.grade(pool[1]).letter != "?"


def test_age_at_or_under_the_target_ties_at_the_top(monkeypatch):
    """Beating 25 years is not a competition; the other components decide it."""
    monkeypatch.setenv("DEPAS_AGE_TARGET", "25")
    pool = [_listing(age=age) for age in (2, 25, 30)]
    scale = Scale(pool, prefs())

    brand_new, target, over = (scale.grade(row).score for row in pool)

    assert brand_new == target > over


def test_under_25_years_is_the_target_with_nothing_configured():
    """The rule stands whether or not DEPAS_AGE_TARGET was ever set."""
    pool = [_listing(age=age) for age in (10, 24, 40)]
    scale = Scale(pool, prefs())

    young, still_under, over = (scale.grade(row).score for row in pool)

    assert young == still_under > over
