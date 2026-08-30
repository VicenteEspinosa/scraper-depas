import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from depas.config import _load_env_file, optional_int, optional_text, target_cost

# Amenities a listing is credited for having; the raw score is the fraction present.
AMENITIES = (
    "has_elevator", "has_concierge", "has_heating", "has_air_conditioning",
    "has_pool", "has_gym", "has_terrace", "gated_community", "pets_allowed",
)
COMPONENTS = ("value", "cost", "location", "size", "amenities", "security", "floor")
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


def _value(row: dict) -> float | None:
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


def _cost(row: dict) -> float | None:
    net = row.get("net_monthly_clp")
    if not net:
        return None
    return _against_target(net, target_cost(), optional_int("DEPAS_ALERT_MAX_COST"))


def _location(row: dict) -> float | None:
    walk = row.get("walk_minutes")
    if walk is None:
        return None
    return _against_target(walk, optional_int("DEPAS_TARGET_WALK"),
                           optional_int("DEPAS_ALERT_MAX_WALK"))


def _size(row: dict) -> float | None:
    """Bigger is better up to DEPAS_TARGET_AREA, where listings tie at the top.

    An undeclared area scores as badly as the smallest allowed size rather than skipping
    the component: a listing that omits its size must not outrank one that states it.
    """
    target = optional_int("DEPAS_TARGET_AREA")
    area = row.get("area")
    if target is None:
        return area or None
    if area is None:
        return -1.0
    minimum = optional_int("DEPAS_ALERT_MIN_AREA")
    span = target - minimum if minimum and minimum < target else target
    return -min(max(target - area, 0.0) / span, 1.0)


def _amenities(row: dict) -> float | None:
    present = [row.get(name) for name in AMENITIES]
    if all(value is None for value in present):
        return None
    return sum(bool(value) for value in present) / len(AMENITIES)


def _security(row: dict) -> float | None:
    """Wanted security is a preference, not a cutoff: missing it costs score, not the alert.

    An undeclared type counts as unmet rather than unknown — otherwise listings that
    simply omit the field would skip the component and outrank ones that state it.
    """
    wanted = optional_text("DEPAS_ALERT_SECURITY")
    if wanted is None:
        return None
    return float(row.get("security_type") == wanted)


# The top floor takes the roof's heat and its leaks, so it is docked on top of any
# shortfall — a penthouse is still worse than the identical unit one floor down.
TOP_FLOOR_PENALTY = 0.5


def _floor(row: dict) -> float | None:
    """Height is a preference, not a cutoff: below the target costs score, and so does the top."""
    floor = row.get("floor")
    if floor is None:
        return None
    target = optional_int("DEPAS_TARGET_FLOOR")
    shortfall = 0.0 if target is None or floor >= target else (target - floor) / target
    top = TOP_FLOOR_PENALTY if floor == row.get("building_floors") else 0.0
    return -min(shortfall + top, 1.0)


RAW = {"value": _value, "cost": _cost, "location": _location, "size": _size,
       "amenities": _amenities, "security": _security, "floor": _floor}


def _weights() -> dict[str, float]:
    _load_env_file()
    weights = {}
    for name in COMPONENTS:
        raw = os.environ.get(f"DEPAS_WEIGHT_{name.upper()}", "1")
        try:
            weights[name] = float(raw)
        except ValueError:
            raise ValueError(f"DEPAS_WEIGHT_{name.upper()} must be a number, got {raw!r}") from None
    if sum(weights.values()) <= 0:
        raise ValueError("at least one DEPAS_WEIGHT_* must be positive")
    return weights


def _percentile(sorted_values: list[float], value: float) -> float:
    """Share of the pool this value beats, counting ties as half."""
    if not sorted_values:
        return 50.0
    below = bisect_left(sorted_values, value)
    ties = bisect_right(sorted_values, value) - below
    return 100.0 * (below + ties / 2) / len(sorted_values)


class Scale:
    """Percentile breakpoints built from the listings currently in the database."""

    def __init__(self, rows: list[dict]) -> None:
        self.weights = _weights()
        self.components = {
            name: sorted(value for row in rows if (value := RAW[name](row)) is not None)
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
            value = RAW[name](row)
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
