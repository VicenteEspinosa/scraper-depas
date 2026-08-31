import json
import re
from datetime import UTC, datetime
from collections.abc import Iterator
from typing import Any

from selectolax.parser import HTMLParser

from depas.communes import Commune
from depas.detail import available_on
from depas.fetch import Fetcher
from depas.models import Listing, Query

NAME = "houm"
API = "https://apis.houm.com/backend/properties/marketplace/"
SITE = "https://houm.com/cl"
PAGE_SIZE = 20
OPERATION_FLAG = {"rent": "for_rental", "sale": "for_sale"}
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
LISTING_URL = re.compile(r"https?://(?:www\.)?houm\.com/\S*?/(\d{4,})\b\S*")


def listing_id(url: str) -> str | None:
    match = LISTING_URL.search(url)
    return match.group(1) if match else None


def fetch_standalone(fetcher: Fetcher, url: str) -> Listing | None:
    """Build a Listing from a Houm detail page, for a link we have never scraped."""
    identifier = listing_id(url)
    match = NEXT_DATA.search(fetcher.get(url).text)
    if identifier is None or match is None:
        return None
    page = json.loads(match.group(1))["props"]["pageProps"]
    property_data = page.get("property")
    if not property_data:
        return None
    return _parse_result({**property_data, "id": int(identifier)}, None)

# Houm stores commune names unaccented and title-cased, which is exactly what the
# enum slugs already are once hyphens become spaces.
def commune_name(commune: Commune) -> str:
    return commune.value.replace("-", " ").title()


def detail_url(commune: Commune, property_id: int) -> str:
    return f"{SITE}/arriendo-departamento-region-metropolitana/{commune.value}/{property_id}"


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    for commune in query.communes or [None]:
        yield from _search_commune(fetcher, query, commune)


def _search_commune(fetcher: Fetcher, query: Query, commune: Commune | None) -> Iterator[Listing]:
    params: dict[str, str] = {
        OPERATION_FLAG[query.operation]: "true",
        "type": "departamento",
        "country": "Chile",
        "limit": str(PAGE_SIZE),
    }
    if commune is not None:
        params["comuna"] = commune_name(commune)

    for page in range(1, query.max_pages + 1):
        payload = fetcher.get(API, params={**params, "page": str(page)}).json()
        for item in payload["results"]:
            listing = _parse_result(item, commune)
            if listing is not None:
                yield listing
        if not payload.get("next"):
            return


def _parse_result(item: dict[str, Any], commune: Commune | None) -> Listing | None:
    price = _default_price(item.get("price") or [])
    details = (item.get("property_details") or [{}])[0]
    if price is None or not item.get("id"):
        return None

    amount, currency = price
    street = " ".join(part for part in (item.get("address"), item.get("street_number")) if part)
    photos = item.get("photos") or []
    return Listing(
        portal=NAME,
        external_id=str(item["id"]),
        url=detail_url(commune or Commune(_slugify(item["comuna"])), item["id"]),
        title=street or item.get("comuna"),
        price=amount,
        currency=currency,
        bedrooms=details.get("dormitorios"),
        bathrooms=details.get("banos"),
        area_m2=details.get("m_construidos"),
        commune=commune.value if commune else _slugify(item.get("comuna") or ""),
        address=street or None,
        image_url=photos[0].get("url") if photos else None,
    )


def _default_price(prices: list[dict[str, Any]]) -> tuple[float, str] | None:
    """Houm quotes every listing in both CLP and CLF; take the one it marks default."""
    for entry in prices:
        if entry.get("currency") == "CLP" and entry.get("value"):
            return float(entry["value"]), "CLP"
    for entry in prices:
        if entry.get("currency") == "CLF" and entry.get("value"):
            return float(entry["value"]), "UF"
    return None


def _slugify(name: str) -> str:
    table = str.maketrans("áéíóúñÁÉÍÓÚÑ", "aeiounAEIOUN")
    return name.translate(table).lower().replace(" ", "-")


UNIT_DIGITS = re.compile(r"(\d{3,4})")


def _floor(property_data: dict[str, Any]) -> int | None:
    """Houm rarely fills apartment_floor; a 3-4 digit unit number encodes it by convention."""
    declared = property_data.get("apartment_floor")
    # values in the thousands are construction years typed into the wrong field
    if isinstance(declared, int) and 0 < declared < 100:
        return declared
    match = UNIT_DIGITS.search(str(property_data.get("numero_depto") or ""))
    return int(match.group(1)[:-2]) or None if match else None


def _published_days_ago(property_data: dict[str, Any]) -> int | None:
    raw = property_data.get("first_publication_date")
    if not raw:
        return None
    published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return (datetime.now(UTC) - published).days


def _benchmark(property_data: dict[str, Any], area: float | None) -> dict[str, float]:
    """Houm publishes its own recommended price, which reads like the zone benchmark."""
    if not area:
        return {}
    uf = next((entry for entry in property_data.get("price") or []
               if entry.get("currency") == "CLF"), None)
    if not uf or not uf.get("value") or not uf.get("recommended_value"):
        return {}
    return {"price_per_m2_uf": round(uf["value"] / area, 4),
            "zone_price_per_m2_uf": round(uf["recommended_value"] / area, 4)}


def fetch_detail(fetcher: Fetcher, url: str) -> dict[str, Any]:
    """Read the full property object the detail page embeds in __NEXT_DATA__."""
    match = NEXT_DATA.search(fetcher.get(url).text)
    if match is None:
        return {}
    page = json.loads(match.group(1))["props"]["pageProps"]
    property_data = page.get("property")
    if not property_data:
        return {}

    details = (property_data.get("property_details") or [{}])[0]
    amenities = property_data.get("association_amenities") or {}
    detail: dict[str, Any] = {
        "common_expenses": details.get("gc") or None,
        "area_useful_m2": details.get("m_construidos"),
        "area_total_m2": details.get("m_terreno"),
        "terrace_m2": details.get("terrace_size"),
        "bedrooms": details.get("dormitorios"),
        "bathrooms": details.get("banos"),
        "parking_spaces": details.get("estacionamientos"),
        "storage_units": details.get("warehouse_quantity"),
        "orientation": details.get("orientacion"),
        "available_from": available_on(details.get("first_available_handover_date") or ""),
        "furnished": int(details.get("furnished") not in (None, "non")),
        "pets_allowed": int(bool(details.get("mascotas"))),
        "has_terrace": int(bool(details.get("terraza"))),
        "lat": details.get("latitud"),
        "lon": details.get("longitud"),
        "description": details.get("observaciones") or None,
        "has_elevator": int(bool(amenities.get("has_elevator"))),
        "has_concierge": int(bool(amenities.get("has_concierge"))),
        "has_pool": int(bool(amenities.get("has_swimming_pool"))),
        "has_gym": int(bool(amenities.get("has_gym"))),
        "security_type": "24 horas" if amenities.get("has_all_day_vigilance") else None,
        "floor": _floor(property_data),
        "published_days_ago": _published_days_ago(property_data),
        "broker": "Houm",
        **_benchmark(property_data, details.get("m_construidos")),
        "features": json.dumps(
            {key: value for key, value in amenities.items() if value is True}, sort_keys=True
        ),
    }
    return {key: value for key, value in detail.items() if value is not None}
