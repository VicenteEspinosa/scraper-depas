"""The apartment you live in today, shaped like a listing so the two can be compared."""
import json
import sqlite3
from datetime import date

from depas.commute import from_listing
from depas.config import current_home, home_net_monthly_clp
from depas.fetch import Fetcher
from depas.metro import nearest_station


def _age(home: dict) -> int | None:
    """Antigüedad, read as a year when it is too large to be a number of years."""
    stated = home.get("age_years")
    if stated is None:
        return None
    return max(date.today().year - stated, 0) if stated > 100 else stated


def row(connection: sqlite3.Connection, fetcher: Fetcher) -> dict | None:
    """Your place as a `listings_ranked` row, so one renderer and one Scale cover both."""
    home = current_home()
    if home is None:
        return None
    uf = connection.execute("SELECT value FROM uf_daily ORDER BY day DESC LIMIT 1").fetchone()
    zone = connection.execute(
        "SELECT uf_per_m2 FROM zone_benchmark WHERE commune = ?", (home.get("commune"),)
    ).fetchone()
    station, _, walk = nearest_station(home["lat"], home["lon"])
    return home | {
        "area": home["area_m2"],
        "age": _age(home),
        "total_monthly_clp": home["price_clp"] + home["common_expenses"],
        "net_monthly_clp": home_net_monthly_clp(home),
        "price_per_m2_uf_effective": (home["price_clp"] / uf[0] / home["area_m2"]
                                      if uf else None),
        "zone_price_per_m2_uf_effective": zone[0] if zone else None,
        "nearest_station": station,
        "walk_minutes": walk,
        "commute": json.dumps(from_listing(fetcher, home["lat"], home["lon"])),
    }
