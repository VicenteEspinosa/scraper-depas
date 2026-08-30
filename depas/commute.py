import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi.requests.exceptions import RequestException

from depas.config import Location, locations
from depas.fetch import Fetcher
from depas.metro import (DETOUR_FACTOR, STATION_COORDS, STATION_LINES, WALK_SPEED_M_PER_MIN,
                         haversine_m, nearest_station)

# Transitous routes over Santiago's whole Red network, buses included, from the DTPM
# feed. It is community-run and best-effort, so every answer is cached and the offline
# estimate below stands in whenever it cannot answer.
ROUTER = "https://api.transitous.org/api/v1/plan"
# Their terms ask callers to identify themselves rather than arrive anonymously.
USER_AGENT = "scraper-depas/1.0 (+https://github.com/VicenteEspinosa/scraper-depas)"
SANTIAGO = ZoneInfo("America/Santiago")

# Metro de Santiago's commercial speed, stops included, and how much longer the track
# runs than the straight line between two stations.
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


def from_listing(fetcher: Fetcher, lat: float, lon: float) -> dict[str, int]:
    """Minutes from one listing to every configured location."""
    travel = {}
    for place in locations():
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
