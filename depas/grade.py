"""What a listing is worth to you, measured against your preferences (docs/DESIGN.md)."""
import json
from dataclasses import dataclass
from datetime import date

from depas.metro import STATION_LINES

# Exactly the components that carry a DEPAS_*_WEIGHT, so there is no second list to keep.
from depas.preferences import WEIGHTED as COMPONENTS
from depas.preferences import Preferences

# Amenities a listing is credited for having; how many of them you expect is a setting.
AMENITIES = (
    "has_elevator", "has_concierge", "has_heating", "has_air_conditioning",
    "has_pool", "has_gym", "has_terrace", "gated_community", "pets_allowed",
)
LETTERS = ((80, "A"), (68, "B"), (56, "C"), (40, "D"))

# The three anchors every component is scored between, in points.
MET = 80.0         # exactly on your target
BREACHED = 40.0    # on the hard bound, one span the wrong side of the target
BEST = 100.0       # one span the right side of it
# Paying your zone's average UF/m2 is MET; this much off that average is a whole span.
ZONE_SPAN = 0.20
# Days past the date you want that cost a whole span; the early side has no fixed span.
LATE_SPAN = 7
# What meeting every target on complete data is worth on top of the components.
PERFECT_BONUS = 5.0


@dataclass(slots=True)
class Grade:
    """A listing's score against your preferences, plus the parts and the flags behind it."""

    score: int
    letter: str
    parts: dict[str, int]
    missing: tuple[str, ...]
    meets_targets: bool


def _points(overshoot: float) -> float:
    """Score one component from how far past your target it sits, counted in spans."""
    if overshoot < 0:
        return min(MET - (BEST - MET) * overshoot, BEST)
    return max(MET - (MET - BREACHED) * overshoot, 0.0)


def _from_target(value: float, target: int, limit: int | None) -> float:
    """Score a lower-is-better number; the distance to the hard bound is one span."""
    # With no bound the target itself is the span, so twice the target scores nothing.
    span = abs(limit - target) if limit is not None and limit != target else target
    return _points((value - target) / span)


def _value(row: dict, prefs: Preferences) -> float | None:
    """Priced against its own commune: the zone average is MET, cheaper beats it."""
    asking = row.get("price_per_m2_uf_effective")
    zone = row.get("zone_price_per_m2_uf_effective")
    if not asking or not zone:
        return None
    return _points((asking / zone - 1) / ZONE_SPAN)


def _cost(row: dict, prefs: Preferences) -> float | None:
    net = row.get("net_monthly_clp")
    if not net or prefs.cost.target is None:
        return None
    return _from_target(net, prefs.cost.target, prefs.cost.maximum)


def _walk(row: dict, prefs: Preferences) -> float | None:
    walk = row.get("walk_minutes")
    if walk is None or prefs.walk.target is None:
        return None
    return _from_target(walk, prefs.walk.target, prefs.walk.maximum)


def _area(row: dict, prefs: Preferences) -> float | None:
    """Bigger is better, so what is scored is how far short of the target it falls."""
    target = prefs.area.target
    if target is None or row.get("area") is None:
        return None
    minimum = prefs.area.minimum
    span = target - minimum if minimum and minimum < target else target
    return _points((target - row["area"]) / span)


def _amenities(row: dict, prefs: Preferences) -> float | None:
    """Scored against how many you expect, so having more than that still pays."""
    target = prefs.value("DEPAS_AMENITIES_TARGET")
    present = [row.get(name) for name in AMENITIES]
    if not target or all(value is None for value in present):
        return None
    return _points((target - sum(bool(value) for value in present)) / target)


def _security(row: dict, prefs: Preferences) -> float | None:
    """Wanted security is a preference, not a cutoff: missing it costs score, not the alert."""
    wanted = prefs.security_wanted()
    if wanted is None or row.get("security_type") is None:
        return None
    return BEST if row.get("security_type") == wanted else BREACHED


def _floor(row: dict, prefs: Preferences) -> float | None:
    """Height is a preference, not a cutoff: below the target costs score, and so does the top."""
    floor, target = row.get("floor"), prefs.floor.target
    if floor is None or target is None:
        return None
    return max(_points((target - floor) / target) - _levied(row, prefs, "floor"), 0.0)


def _age(row: dict, prefs: Preferences) -> float | None:
    """An undeclared antigüedad is left unscored rather than assumed old: most portals omit it."""
    age = row.get("age")
    if age is None or prefs.age.target is None:
        return None
    return _from_target(float(age), prefs.age.target, prefs.age.maximum)


def _availability(row: dict, prefs: Preferences) -> float | None:
    """Scored on how close the entrega lands to the date you want, from either side."""
    configured, stated = prefs.value("DEPAS_AVAILABILITY_TARGET"), row.get("available_from")
    if configured is None or not stated:
        return None
    today, wanted = date.today(), date.fromisoformat(configured)
    # A date already reached is entrega inmediata, not however long ago it was written.
    frees_up = max(date.fromisoformat(stated), today)
    # Early is measured against the whole window you are shopping in, never a fixed span.
    span = (wanted - today).days if frees_up < wanted else LATE_SPAN
    return _points(abs((frees_up - wanted).days) / span - 1)


def _metro(row: dict, prefs: Preferences) -> float | None:
    """Your top tier is BEST, the next one MET, and the rest drop evenly to an unranked line."""
    tiers = prefs.metro_tiers()
    station = row.get("nearest_station")
    if not tiers or station is None:
        return None
    lines = STATION_LINES.get(station)
    if not lines:
        return None
    # A line nobody ranked sits one tier worse than the last one that was.
    ranks = [rank for rank, tier in enumerate(tiers) if any(line in tier for line in lines)]
    return _points(3 * (min(ranks) if ranks else len(tiers)) / len(tiers) - 1)


def _commute(row: dict, prefs: Preferences) -> float | None:
    """Judged on the location it reaches worst — you have to make every one of those trips."""
    travel = row.get("commute")
    if not travel or prefs.commute.target is None:
        return None
    return _from_target(max(json.loads(travel).values()),
                        prefs.commute.target, prefs.commute.maximum)


def _levied(row: dict, prefs: Preferences, component: str) -> float:
    """Points the penalised traits assigned to one component cost this listing."""
    return sum(trait.penalty for trait in prefs.penalised_traits()
               if trait.component == component and trait.holds(row))


def _traits(row: dict, prefs: Preferences) -> float | None:
    """BEST less what the penalised traits this listing carries are each worth."""
    penalised = [trait for trait in prefs.penalised_traits() if trait.component == "traits"]
    # Traits come off the detail page: unread, a listing has answered nothing, not "no".
    if not penalised or not row.get("detail_fetched_at"):
        return None
    return max(BEST - _levied(row, prefs, "traits"), 0.0)


# One shape for all of them, so the dispatch below needs no special case.
SCORERS = {"value": _value, "cost": _cost, "walk": _walk, "area": _area,
           "amenities": _amenities, "security": _security, "floor": _floor, "metro": _metro,
           "commute": _commute, "age": _age, "availability": _availability,
           "traits": _traits}


def _applicable(prefs: Preferences) -> set[str]:
    """Components you have given something to score against; the rest are simply off."""
    configured = {
        "value": True,
        "cost": prefs.cost.target is not None,
        "walk": prefs.walk.target is not None,
        "area": prefs.area.target is not None,
        "amenities": bool(prefs.value("DEPAS_AMENITIES_TARGET")),
        "security": prefs.security_wanted() is not None,
        "floor": prefs.floor.target is not None,
        "metro": bool(prefs.metro_tiers()),
        "commute": prefs.commute.target is not None,
        "age": prefs.age.target is not None,
        "availability": prefs.value("DEPAS_AVAILABILITY_TARGET") is not None,
        "traits": any(trait.component == "traits" for trait in prefs.penalised_traits()),
    }
    return {name for name in COMPONENTS if configured[name]}


class Scale:
    """Your preferences, ready to grade rows against. Deterministic: no listing sees another."""

    def __init__(self, prefs: Preferences) -> None:
        self.prefs = prefs
        self.weights = prefs.weights()
        self.applicable = _applicable(prefs)

    def _parts(self, row: dict) -> dict[str, float]:
        return {name: scored for name in COMPONENTS
                if (scored := SCORERS[name](row, self.prefs)) is not None}

    def grade(self, row: dict) -> Grade:
        """Score one listing out of 100, where 80 is everything you asked for and no more."""
        parts = self._parts(row)
        if not parts:
            return Grade(0, "?", {}, COMPONENTS, False)
        weight = sum(self.weights[name] for name in parts)
        # The only thing that punishes silence: averaging what scored would hide it.
        average = (sum(parts[name] * self.weights[name] for name in parts) / weight
                   if weight else BREACHED)
        coverage = len(parts) / len(self.applicable) if self.applicable else 1.0
        missing = tuple(name for name in self.applicable if name not in parts)
        meets_targets = all(scored >= MET for scored in parts.values())
        # Only a listing that also answered everything gets the bonus.
        score = round(BREACHED + (average - BREACHED) * coverage
                      + (PERFECT_BONUS if meets_targets and not missing else 0.0))
        letter = next((letter for cutoff, letter in LETTERS if score >= cutoff), "E")
        return Grade(score, letter, {name: round(scored) for name, scored in parts.items()},
                     missing, meets_targets)
