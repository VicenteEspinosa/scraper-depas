import pytest

from depas.grade import COMPONENTS, Scale


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
    for component in COMPONENTS:
        monkeypatch.setenv(f"DEPAS_WEIGHT_{component.upper()}", "0")

    with pytest.raises(ValueError, match="at least one"):
        Scale(_pool())


def test_security_is_scored_not_filtered(monkeypatch):
    """Wanting 24h security lowers the score of listings without it, never excludes them."""
    monkeypatch.setenv("DEPAS_ALERT_SECURITY", "24 horas")
    pool = [{"security_type": s, "net_monthly_clp": 700_000, "area": 50.0, "walk_minutes": 5}
            for s in ("24 horas", None, "conserje diurno")]
    scale = Scale(pool)

    wanted, absent, other = (scale.grade(row) for row in pool)

    assert wanted.score > absent.score
    assert absent.score == other.score
    assert all(g.letter != "?" for g in (wanted, absent, other))


def test_an_unset_security_preference_is_not_a_missing_component(monkeypatch):
    """Without the preference set, no listing should be marked as partially graded for it."""
    monkeypatch.delenv("DEPAS_ALERT_SECURITY", raising=False)
    pool = [{"net_monthly_clp": 700_000, "area": 50.0, "walk_minutes": 5, "has_elevator": 1}]

    assert "security" not in Scale(pool).grade(pool[0]).missing


def test_floor_below_target_scores_lower_without_being_excluded(monkeypatch):
    """A low floor costs score, so listings on it still alert rather than disappearing."""
    monkeypatch.setenv("DEPAS_TARGET_FLOOR", "5")
    pool = [_listing(floor=floor, building_floors=20) for floor in (8, 5, 3, 1)]
    scale = Scale(pool)

    high, target, low, ground = (scale.grade(row).score for row in pool)

    assert high == target > low > ground
    assert all(scale.grade(row).letter != "?" for row in pool)


def test_the_top_floor_is_docked(monkeypatch):
    """Being the highest floor scores below the identical unit one floor down."""
    monkeypatch.setenv("DEPAS_TARGET_FLOOR", "5")
    pool = [_listing(floor=20, building_floors=20), _listing(floor=19, building_floors=20)]
    scale = Scale(pool)

    top, below = (scale.grade(row).score for row in pool)

    assert top < below


def test_an_unknown_floor_is_missing_not_penalised(monkeypatch):
    """A portal that never publishes a floor must not be graded as if it were floor zero."""
    monkeypatch.setenv("DEPAS_TARGET_FLOOR", "5")
    pool = [_listing(floor=5, building_floors=20), _listing(floor=None)]
    scale = Scale(pool)

    assert "floor" in scale.grade(pool[1]).missing
    assert scale.grade(pool[1]).letter != "?"


def test_area_at_the_target_ties_at_the_top(monkeypatch):
    """Past the ideal size, extra square metres stop earning score."""
    monkeypatch.setenv("DEPAS_TARGET_AREA", "50")
    monkeypatch.setenv("DEPAS_ALERT_MIN_AREA", "42")
    pool = [_listing(area=a) for a in (80.0, 50.0, 46.0, 42.0)]
    scale = Scale(pool)

    huge, target, middling, smallest = (scale.grade(row).score for row in pool)

    assert huge == target > middling > smallest


def test_an_unknown_area_scores_bottom_but_still_grades(monkeypatch):
    """A listing that hides its size must not outrank one that states it, nor be dropped."""
    monkeypatch.setenv("DEPAS_TARGET_AREA", "50")
    monkeypatch.setenv("DEPAS_ALERT_MIN_AREA", "42")
    pool = [_listing(area=50.0), _listing(area=42.0), _listing(area=None)]
    scale = Scale(pool)

    stated, smallest, unknown = (scale.grade(row) for row in pool)

    assert stated.score > unknown.score
    assert unknown.score == smallest.score
    assert "size" not in unknown.missing


def test_a_thin_grade_cannot_outrank_a_complete_one(monkeypatch):
    """Winning four axes must not beat winning the same four plus three more."""
    monkeypatch.setenv("DEPAS_TARGET_FLOOR", "5")
    monkeypatch.setenv("DEPAS_TARGET_AREA", "50")
    monkeypatch.setenv("DEPAS_ALERT_SECURITY", "24 horas")
    shared = {"net_monthly_clp": 500_000, "walk_minutes": 2, "area": 90.0,
              "security_type": "24 horas"}
    complete = shared | {"price_per_m2_uf": 0.2, "zone_price_per_m2_uf": 0.5,
                         "has_elevator": 1, "floor": 8, "building_floors": 20}
    thin = dict(shared)
    filler = [{"net_monthly_clp": 900_000, "walk_minutes": 14, "area": 42.0,
               "price_per_m2_uf": 0.6, "zone_price_per_m2_uf": 0.5, "has_pool": 0,
               "floor": 1, "building_floors": 1, "security_type": None}]
    scale = Scale([complete, thin] + filler)

    assert scale.grade(complete).score > scale.grade(thin).score


def test_coverage_only_pulls_toward_the_middle(monkeypatch):
    """Shrinking must never flip a thin listing below a genuinely worse complete one."""
    monkeypatch.setenv("DEPAS_TARGET_AREA", "50")
    good_thin = {"net_monthly_clp": 500_000, "area": 90.0}
    bad_complete = {"net_monthly_clp": 950_000, "area": 42.0, "walk_minutes": 15,
                    "price_per_m2_uf": 0.9, "zone_price_per_m2_uf": 0.5, "has_pool": 0}
    scale = Scale([good_thin, bad_complete])

    assert scale.grade(good_thin).score > scale.grade(bad_complete).score
