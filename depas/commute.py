import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi.requests.exceptions import RequestException

from depas.config import Location
from depas.fetch import Fetcher
from depas.metro import (
    DETOUR_FACTOR,
    STATION_COORDS,
    STATION_LINES,
    WALK_SPEED_M_PER_MIN,
    haversine_m,
    nearest_station,
)

# Transitous routes Santiago's whole Red network, buses included, from the DTPM feed.
ROUTER = "https://api.transitous.org/api/v1/plan"
# The same service geocodes, so an address needs no second provider to be trusted.
GEOCODER = "https://api.transitous.org/api/v1/geocode"
# Their terms ask callers to identify themselves rather than arrive anonymously.
USER_AGENT = "scraper-depas/1.0 (+https://github.com/VicenteEspinosa/scraper-depas)"
SANTIAGO = ZoneInfo("America/Santiago")

# Metro's commercial speed with stops, and how much longer the track runs than the line.
METRO_SPEED_M_PER_MIN = 580.0
METRO_ROUTE_FACTOR = 1.2
# Waiting for the first train, and again after changing lines.
WAIT_MINUTES = 4.0
TRANSFER_MINUTES = 5.0


def _walk_minutes(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    return haversine_m(lat, lon, to_lat, to_lon) * DETOUR_FACTOR / WALK_SPEED_M_PER_MIN


def _metro_minutes(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """Walk to the closest station, ride, walk off — with a change unless a line runs through."""
    board, _, walk_in = nearest_station(lat, lon)
    exit_station, _, walk_out = nearest_station(to_lat, to_lon)
    ride_m = haversine_m(*STATION_COORDS[board], *STATION_COORDS[exit_station]) * METRO_ROUTE_FACTOR
    direct = set(STATION_LINES[board]) & set(STATION_LINES[exit_station])
    return (walk_in + WAIT_MINUTES + ride_m / METRO_SPEED_M_PER_MIN
            + (0.0 if direct else TRANSFER_MINUTES) + walk_out)


def estimated_minutes(lat: float, lon: float, to_lat: float, to_lon: float) -> int:
    """Offline fallback: the faster of walking and the Metro, blind to every bus."""
    return round(min(_walk_minutes(lat, lon, to_lat, to_lon),
                     _metro_minutes(lat, lon, to_lat, to_lon)))


def coordinates(fetcher: Fetcher, address: str) -> tuple[float, float, str]:
    """Where an address is, plus the place the geocoder actually matched it to."""
    response = fetcher.get(GEOCODER, params={"text": address, "language": "es"},
                           headers={"User-Agent": USER_AGENT})
    matches = [found for found in response.json() if found.get("type") != "STOP"]
    if not matches:
        raise ValueError(f"no place found for {address!r}")
    # A street and number is meant literally, so a real address beats a similar landmark.
    best = next((found for found in matches if found.get("type") == "ADDRESS"), matches[0])
    where = ", ".join(area["name"] for area in best.get("areas", []) if area.get("default"))
    return best["lat"], best["lon"], f"{best['name']}{f', {where}' if where else ''}"


def _coordinates_already(parts: list[str]) -> bool:
    if len(parts) != 3:
        return False
    try:
        float(parts[1]), float(parts[2])
    except ValueError:
        return False
    return True


def resolve_locations(fetcher: Fetcher, raw: str) -> tuple[str, list[str]]:
    """Turn any `name,address` entries into `name,lat,lon`, reporting what each matched."""
    resolved, matched = [], []
    for entry in raw.split(";"):
        parts = [part.strip() for part in entry.split(",")]
        if not any(parts):
            continue
        if _coordinates_already(parts):
            resolved.append(",".join(parts))
            continue
        name, address = parts[0], ", ".join(parts[1:]).strip()
        if not address:
            raise ValueError(f"{name!r} needs an address or a lat,lon")
        lat, lon, where = coordinates(fetcher, address)
        resolved.append(f"{name},{lat:.5f},{lon:.5f}")
        matched.append(f"{name} → {where}")
    return "; ".join(resolved), matched


def next_weekday_morning() -> str:
    """A commute is a weekday-morning trip, and a fixed one keeps listings comparable."""
    now = datetime.now(SANTIAGO)
    monday = now + timedelta(days=(7 - now.weekday()) or 7)
    return monday.replace(hour=8, minute=30, second=0, microsecond=0).isoformat()


def routed_minutes(fetcher: Fetcher, lat: float, lon: float, place: Location) -> int | None:
    """Fastest walk-or-transit trip Transitous knows of, or None when it cannot answer."""
    response = fetcher.get(
        ROUTER,
        params={"fromPlace": f"{lat},{lon}", "toPlace": f"{place.lat},{place.lon}",
                "time": next_weekday_morning(), "numItineraries": 3},
        headers={"User-Agent": USER_AGENT},
    )
    plan = response.json()
    # `direct` is the walk-only trip; `itineraries` are the ones that board something.
    trips = [*plan.get("direct", []), *plan.get("itineraries", [])]
    return round(min(trip["duration"] for trip in trips) / 60) if trips else None


def from_listing(fetcher: Fetcher, lat: float, lon: float,
                 places: Sequence[Location]) -> dict[str, int]:
    """Minutes from one listing to each of the places handed in."""
    travel = {}
    for place in places:
        try:
            routed = routed_minutes(fetcher, lat, lon, place)
        except RequestException:
            # Best-effort service: an hourly pass must not die because it is down.
            routed = None
        travel[place.name] = (estimated_minutes(lat, lon, place.lat, place.lon)
                              if routed is None else routed)
    return travel


def as_text(commute: str | None) -> str:
    """`{"gym": 32}` rendered for a table cell or an alert card; empty when unknown."""
    if not commute:
        return ""
    return " · ".join(f"{name} {travel}" for name, travel in json.loads(commute).items())
