import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from depas.metro import STATION_LINES
# The components are exactly the things that carry a DEPAS_*_WEIGHT, so the registry
# owns the list and this reads it rather than keeping a second copy in step.
from depas.preferences import WEIGHTED as COMPONENTS
from depas.preferences import Preferences

# Amenities a listing is credited for having; the raw score is the fraction present.
AMENITIES = (
    "has_elevator", "has_concierge", "has_heating", "has_air_conditioning",
    "has_pool", "has_gym", "has_terrace", "gated_community", "pets_allowed",
)
LETTERS = ((90, "A"), (75, "B"), (50, "C"), (25, "D"))
# A percentile scale centres here, so it is what an unmeasured component says.
MIDPOINT = 50.0


@dataclass(slots=True)
class Grade:
    """A listing's 0-100 percentile against the current pool, plus the parts behind it."""

    score: int
    letter: str
    parts: dict[str, int]
    missing: tuple[str, ...]


def _value(row: dict, prefs: Preferences) -> float | None:
    """Cheaper than its zone scores better, so the ratio is negated."""
    asking = row.get("price_per_m2_uf_effective")
    zone = row.get("zone_price_per_m2_uf_effective")
    return None if not asking or not zone else -(asking / zone)


def _against_target(value: float, target: int | None, ceiling: int | None) -> float:
    """Lower is better. Meeting the target ties at the top; past it, score falls to the ceiling.

    Beating a target is not a competition — once a listing qualifies the other components
    should decide it. Without a target this is just the negated value, i.e. a percentile.
    """
    if target is None:
        return -value
    if value <= target:
        return 0.0
    span = (ceiling - target) if ceiling and ceiling > target else target
    return -min((value - target) / span, 1.0)


def _cost(row: dict, prefs: Preferences) -> float | None:
    net = row.get("net_monthly_clp")
    if not net:
        return None
    return _against_target(net, prefs.cost.target, prefs.cost.maximum)


def _walk(row: dict, prefs: Preferences) -> float | None:
    walk = row.get("walk_minutes")
    if walk is None:
        return None
    return _against_target(walk, prefs.walk.target, prefs.walk.maximum)


def _area(row: dict, prefs: Preferences) -> float | None:
    """Bigger is better up to DEPAS_AREA_TARGET, where listings tie at the top.

    An undeclared area scores as badly as the smallest allowed size rather than skipping
    the component: a listing that omits its size must not outrank one that states it.
    """
    target = prefs.area.target
    area = row.get("area")
    if target is None:
        return area or None
    if area is None:
        return -1.0
    minimum = prefs.area.minimum
    span = target - minimum if minimum and minimum < target else target
    return -min(max(target - area, 0.0) / span, 1.0)


def _amenities(row: dict, prefs: Preferences) -> float | None:
    present = [row.get(name) for name in AMENITIES]
    if all(value is None for value in present):
        return None
    return sum(bool(value) for value in present) / len(AMENITIES)


def _security(row: dict, prefs: Preferences) -> float | None:
    """Wanted security is a preference, not a cutoff: missing it costs score, not the alert.

    An undeclared type counts as unmet rather than unknown — otherwise listings that
    simply omit the field would skip the component and outrank ones that state it.
    """
    wanted = prefs.security_wanted()
    if wanted is None:
        return None
    return float(row.get("security_type") == wanted)


# The top floor takes the roof's heat and its leaks, so it is docked on top of any
# shortfall — a penthouse is still worse than the identical unit one floor down.
TOP_FLOOR_PENALTY = 0.5


def _floor(row: dict, prefs: Preferences) -> float | None:
    """Height is a preference, not a cutoff: below the target costs score, and so does the top."""
    floor = row.get("floor")
    if floor is None:
        return None
    target = prefs.floor.target
    shortfall = 0.0 if target is None or floor >= target else (target - floor) / target
    top = TOP_FLOOR_PENALTY if floor == row.get("building_floors") else 0.0
    return -min(shortfall + top, 1.0)


def _age(row: dict, prefs: Preferences) -> float | None:
    """Newer is better up to the age target; past it the score falls without excluding.

    An undeclared antigüedad is left unscored rather than assumed old: most portals
    simply omit it. Age declares no MAX slot because it never blocks an alert — the
    target alone sets how fast an older building loses the component.
    """
    age = row.get("age")
    if age is None:
        return None
    return _against_target(float(age), prefs.age.target, prefs.age.maximum)


def _metro(row: dict, prefs: Preferences) -> float | None:
    """Rank the station by the best-tiered line calling at it; an interchange takes its best."""
    tiers = prefs.metro_tiers()
    station = row.get("nearest_station")
    if not tiers or station is None:
        return None
    lines = STATION_LINES.get(station)
    if not lines:
        return None
    # A line nobody ranked sits one tier worse than the last one that was.
    ranks = [rank for rank, tier in enumerate(tiers) if any(line in tier for line in lines)]
    return -(min(ranks) if ranks else len(tiers)) / len(tiers)


def _commute(row: dict, prefs: Preferences) -> float | None:
    """Judged on the location it reaches worst — you have to make every one of those trips."""
    travel = row.get("commute")
    if not travel:
        return None
    return _against_target(max(json.loads(travel).values()),
                           prefs.commute.target, prefs.commute.maximum)


# One shape for all of them, preferences included, so the dispatch below needs no
# special case for the two that happen not to consult any.
RAW = {"value": _value, "cost": _cost, "walk": _walk, "area": _area,
       "amenities": _amenities, "security": _security, "floor": _floor, "metro": _metro,
       "commute": _commute, "age": _age}


def _percentile(sorted_values: list[float], value: float) -> float:
    """Share of the pool this value beats, counting ties as half."""
    if not sorted_values:
        return 50.0
    below = bisect_left(sorted_values, value)
    ties = bisect_right(sorted_values, value) - below
    return 100.0 * (below + ties / 2) / len(sorted_values)


class Scale:
    """Percentile breakpoints built from the listings currently in the database."""

    def __init__(self, rows: list[dict], prefs: Preferences) -> None:
        # The scale belongs to whoever is reading it: the same pool graded against two
        # sets of targets is two different scales, so the preferences come in with it.
        self.prefs = prefs
        self.weights = prefs.weights()
        self.components = {
            name: sorted(value for row in rows if (value := RAW[name](row, prefs)) is not None)
            for name in COMPONENTS
        }
        # `security` scores nothing unless the preference is set; treating that as
        # missing data would put a partial-data mark on every listing.
        self.applicable = {name for name, values in self.components.items() if values}
        self.composites = sorted(self._composite(row) for row in rows) if rows else []

    def _composite(self, row: dict) -> float:
        """Weighted mean of the scored components, shrunk toward the middle by coverage."""
        # Averaging only what is present renormalises missing data away, so a listing
        # scored on four axes it happens to win ties with one scored on all seven.
        parts = self._parts(row)
        if not parts:
            return MIDPOINT
        weight = sum(self.weights[name] for name in parts)
        if weight == 0:
            return MIDPOINT
        average = sum(parts[name] * self.weights[name] for name in parts) / weight
        coverage = len(parts) / len(self.applicable) if self.applicable else 1.0
        return MIDPOINT + (average - MIDPOINT) * coverage

    def _parts(self, row: dict) -> dict[str, float]:
        parts = {}
        for name in COMPONENTS:
            value = RAW[name](row, self.prefs)
            if value is not None:
                parts[name] = _percentile(self.components[name], value)
        return parts

    def grade(self, row: dict) -> Grade:
        """Rank one listing against the pool; 80 means it beats 80% of what is listed."""
        parts = self._parts(row)
        if not parts:
            return Grade(0, "?", {}, COMPONENTS)
        score = round(_percentile(self.composites, self._composite(row)))
        letter = next((letter for cutoff, letter in LETTERS if score >= cutoff), "E")
        missing = tuple(name for name in self.applicable if name not in parts)
        return Grade(score, letter, {k: round(v) for k, v in parts.items()}, missing)
