import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from curl_cffi.requests.exceptions import HTTPError

from depas.communes import Commune
from depas.detail import parse_specs
from depas.fetch import Fetcher
from depas.models import Currency, Listing, Query

NAME = "toctoc"
SITE = "https://www.toctoc.com"
OPERATION_PATH = {"rent": "arriendo", "sale": "venta"}
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
LISTING_URL = re.compile(r"https?://(?:www\.)?toctoc\.com/\S*?/[a-z]_[0-9a-f]{40}\S*")
LISTING_ID = re.compile(r"/[a-z]_([0-9a-f]{40})")

# TocToc labels the same specs Portal Inmobiliario does, just abbreviated.
SPEC_LABELS = {"Superf. útil": "Superficie útil", "Superf. terraza": "Superficie de terraza"}


def listing_id(url: str) -> str | None:
    match = LISTING_ID.search(url)
    return match.group(1) if match else None


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    for commune in query.communes or [None]:
        yield from _search_commune(fetcher, query, commune)


def _search_commune(fetcher: Fetcher, query: Query, commune: Commune | None) -> Iterator[Listing]:
    path = [SITE, OPERATION_PATH[query.operation], "departamento"]
    if commune is not None:
        path.append(commune.value)
    try:
        page = _page_props(fetcher, "/".join(path))
    except HTTPError as error:
        if error.response.status_code == 404:  # a commune TocToc does not index
            return
        raise
    if page is None:
        return

    # The list page server-renders its whole first slice at once, so query.max_pages
    # has nothing to page through.
    for item in page["propiedades"]["results"]:
        listing = _parse_result(item)
        if listing is not None:
            yield listing


def _parse_result(item: dict[str, Any]) -> Listing | None:
    price = _price(item.get("precios") or [])
    url = item.get("urlFicha")
    if price is None or not url:
        return None

    amount, currency = price
    image = item.get("imagenPrincipal") or {}
    return Listing(
        portal=NAME,
        external_id=item["hashId"],
        url=url,
        title=item.get("titulo"),
        price=amount,
        currency=currency,
        bedrooms=_count(item.get("dormitorios") or []),
        bathrooms=_count(item.get("bannos") or []),
        area_m2=_number(item.get("superficie") or []),
        commune=commune_slug(url),
        image_url=image.get("src") or None,
    )


def commune_slug(url: str) -> str:
    """A listing url ends in .../<region>/<commune>/<hash>, and that slug is our own."""
    return url.split("/")[-2]


def _number(values: list[str]) -> float | None:
    """Card figures arrive as one-element string lists, where "0" means undeclared."""
    return float(values[0].replace(",", ".")) or None if values else None


def _count(values: list[str]) -> int | None:
    number = _number(values)
    return int(number) if number else None


def _price(prices: list[dict[str, str]]) -> tuple[float, Currency] | None:
    """TocToc quotes both currencies, but rounds the UF one to whole units."""
    by_prefix = {entry["prefix"]: entry["value"] for entry in prices if entry.get("value")}
    if "$" in by_prefix:
        return float(by_prefix["$"].replace(".", "")), "CLP"
    if "UF" in by_prefix:
        return float(by_prefix["UF"].replace(".", "").replace(",", ".")), "UF"
    return None


def fetch_standalone(fetcher: Fetcher, url: str) -> Listing | None:
    """Build a Listing from a TocToc detail page, for a link we have never scraped."""
    property_data = _property(fetcher, url)
    if property_data is None:
        return None
    price, currency = ((property_data["price"], "CLP") if property_data["price"]
                       else (property_data["priceUf"], "UF"))
    return Listing(
        portal=NAME,
        external_id=property_data["hashId"],
        url=property_data["urlPublication"],
        title=property_data["title"],
        price=float(price),
        currency=currency,
        commune=commune_slug(property_data["urlPublication"]),
    )


def fetch_detail(fetcher: Fetcher, url: str) -> dict[str, Any]:
    """Read the property object the detail page embeds in __NEXT_DATA__."""
    property_data = _property(fetcher, url)
    if property_data is None:
        return {}

    longitude, latitude = property_data["address"]["location"]["coordinates"]
    specs = [(SPEC_LABELS.get(label, label), value)
             for label, value in _characteristics(property_data)]
    detail: dict[str, Any] = {
        **parse_specs(specs),
        "description": property_data.get("description") or None,
        "lat": latitude,
        "lon": longitude,
        "published_days_ago": _published_days_ago(property_data["operation"]["publicationDate"]),
        "broker": property_data["client"]["name"],
    }
    return {key: value for key, value in detail.items() if value is not None}


def _characteristics(property_data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(row["name"].strip(" :"), row["value"].strip())
            for row in property_data.get("characteristics") or []]


def _published_days_ago(published_at: str) -> int:
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return (datetime.now(UTC) - published).days


def _page_props(fetcher: Fetcher, url: str) -> dict[str, Any] | None:
    match = NEXT_DATA.search(fetcher.get(url).text)
    return json.loads(match.group(1))["props"]["pageProps"] if match else None


def _property(fetcher: Fetcher, url: str) -> dict[str, Any] | None:
    page = _page_props(fetcher, url)
    return page["initialState"]["property"]["property"]["data"] if page else None
