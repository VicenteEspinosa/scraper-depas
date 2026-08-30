import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from depas.communes import Commune
from depas.fetch import Fetcher
from depas.models import Listing, Query
from depas.uf import uf_in_clp

NAME = "assetplan"
SITE = "https://www.assetplan.cl"
RENTALS = f"{SITE}/arriendo/departamento"
SEARCH_API = f"{SITE}/api/buildings/search-by-commune"
UNIT_API = f"{RENTALS}/units"
BUILDING_API = f"{SITE}/api/buildings"
LISTING_URL = re.compile(r"https?://(?:www\.)?assetplan\.cl/arriendo/departamento/\S+?/retail/\d+")
LISTING_ID = re.compile(r"/retail/(\d+)")
CSRF_TOKEN = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
# The search page hands its map component the numeric commune id the API asks for.
COMMUNE_ID = re.compile(r"buildingMapSearch\(.*?'[a-z-]+',\s*(\d+)\s*\)", re.S)

ORIENTATIONS = {"N": "Norte", "NO": "NorOriente", "NP": "NorPoniente", "O": "Oriente",
                "P": "Poniente", "S": "Sur", "SO": "SurOriente", "SP": "SurPoniente"}


def listing_id(url: str) -> str | None:
    match = LISTING_ID.search(url)
    return match.group(1) if match else None


def unit_url(commune_slug: str, building_name: str, building_id: int, unit_id: int) -> str:
    """Assetplan's canonical page for one unit; `retail` names the route, not the unit type."""
    return f"{RENTALS}/{commune_slug}/{_slug(building_name)}/{building_id}/retail/{unit_id}"


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    if query.operation != "rent":
        raise NotImplementedError(f"{NAME} publishes rentals only")
    if not query.communes:
        raise NotImplementedError(f"{NAME} searches one commune at a time; pass --commune")
    for commune in query.communes:
        yield from _search_commune(fetcher, commune)


def _search_commune(fetcher: Fetcher, commune: Commune) -> Iterator[Listing]:
    page_url = f"{RENTALS}/{commune.value}"
    page = fetcher.get(page_url).text
    payload = fetcher.post(
        SEARCH_API,
        json={"commune_id": int(COMMUNE_ID.search(page).group(1))},
        headers={"X-CSRF-TOKEN": CSRF_TOKEN.search(page).group(1),
                 "Accept": "application/json", "Referer": page_url},
    ).json()

    # The API answers with the commune's whole inventory at once, so query.max_pages
    # has nothing to page through.
    for building in payload["data"]["buildings"]:
        for unit in _units(building):
            listing = _parse_unit(building, unit)
            if listing is not None:
                yield listing


def _units(building: dict[str, Any]) -> list[dict[str, Any]]:
    """A building Assetplan owns lists its units inline; any other result is itself one unit."""
    grouped = building.get("available_units")
    return grouped if grouped else [{**building, "id": building["unit_id"]}]


def _parse_unit(building: dict[str, Any], unit: dict[str, Any]) -> Listing | None:
    if not unit.get("price"):
        return None
    return Listing(
        portal=NAME,
        external_id=str(unit["id"]),
        url=unit_url(building["commune_slug"], building["nombre"],
                     building["building_id"], unit["id"]),
        title=building["nombre"],
        price=float(unit["price"]),
        currency="CLP",
        bedrooms=unit["bedrooms"],
        bathrooms=unit["bathrooms"],
        area_m2=unit.get("m2_utiles"),
        commune=building["commune_slug"],
        address=building["direccion"],
        image_url=building["photo_urls"][0],
    )


def fetch_standalone(fetcher: Fetcher, url: str) -> Listing | None:
    """Build a Listing from Assetplan's unit endpoint, for a link we have never scraped."""
    identifier = listing_id(url)
    if identifier is None:
        return None
    unit = _unit(fetcher, identifier)
    commune_slug = _slug(unit["building_commune"])
    # price_with_discount is a promotion on one month; the lease runs at monto_depto.
    return Listing(
        portal=NAME,
        external_id=identifier,
        url=unit_url(commune_slug, unit["building_name"], unit["building_id"], unit["id"]),
        title=unit["building_name"],
        price=float(unit["price"]["monto_depto"]),
        currency="CLP",
        bedrooms=unit["typology"]["bedrooms"],
        bathrooms=unit["typology"]["bathrooms"],
        area_m2=float(unit["m2_utiles"]),
        commune=commune_slug,
        address=unit["building"]["direccion"],
        image_url=unit["building_photo"],
    )


def fetch_detail(fetcher: Fetcher, url: str) -> dict[str, Any]:
    """Read the unit's own specs, plus the building coordinates the metro walk needs."""
    unit = _unit(fetcher, LISTING_ID.search(url).group(1))
    coordinates = _building(fetcher, unit["building_id"])["_geo"]
    area = float(unit["m2_utiles"])
    terrace = float(unit["m2_terraza"])
    detail: dict[str, Any] = {
        "area_useful_m2": area,
        "terrace_m2": terrace or None,
        "has_terrace": int(terrace > 0),
        "bedrooms": unit["typology"]["bedrooms"],
        "bathrooms": unit["typology"]["bathrooms"],
        "floor": unit["piso"],
        "orientation": ORIENTATIONS.get(unit["orientacion"]),
        "common_expenses": int(float(unit["ggcc_final"])) or None,
        "parking_spaces": int(unit["has_parking"]),
        "storage_units": int(unit["has_store"]),
        "pets_allowed": int(unit["acepta_mascotas"]),
        "furnished": unit["furnished"],
        "lat": coordinates["lat"],
        "lon": coordinates["lng"],
        "broker": "Assetplan",
        **_benchmark(unit["price"], area, uf_in_clp(fetcher)),
    }
    return {key: value for key, value in detail.items() if value is not None}


def _benchmark(pricing: dict[str, Any], area: float, uf_value: float) -> dict[str, float]:
    """Assetplan publishes the rent it recommends for the unit, which reads like the benchmark."""
    recommended = float(pricing["precio_recomendado"])
    if not recommended:
        return {}
    return {"price_per_m2_uf": round(float(pricing["monto_depto"]) / uf_value / area, 4),
            "zone_price_per_m2_uf": round(recommended / uf_value / area, 4)}


def _unit(fetcher: Fetcher, unit_id: str) -> dict[str, Any]:
    return fetcher.get(f"{UNIT_API}/{unit_id}", headers={"Accept": "application/json"}).json()


def _building(fetcher: Fetcher, building_id: int) -> dict[str, Any]:
    response = fetcher.get(f"{BUILDING_API}/{building_id}", headers={"Accept": "application/json"})
    return response.json()["data"]
