import json
import re
from collections.abc import Iterator

from curl_cffi.requests.exceptions import HTTPError
from selectolax.parser import HTMLParser, Node

from depas.communes import Commune
from depas.detail import _slug, parse_specs, published_days_ago
from depas.fetch import Fetcher
from depas.models import Listing, Query

NAME = "portalinmobiliario"
BASE = "https://www.portalinmobiliario.com"
# Portal Inmobiliario is MercadoLibre's property vertical: same item ids, same
# page markup, so a mercadolibre.cl link parses with exactly these selectors.
LISTING_HOSTS = ("portalinmobiliario.com", "mercadolibre.cl")
REGION = "metropolitana"
PAGE_SIZE = 48
OPERATION_PATH = {"rent": "arriendo", "sale": "venta"}
LISTING_ID = re.compile(r"/(MLC-\d+)")
LEADING_NUMBER = re.compile(r"(\d[\d.]*)")


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    for commune in query.communes or [None]:
        yield from _search_commune(fetcher, query, commune)


def _search_commune(fetcher: Fetcher, query: Query, commune: Commune | None) -> Iterator[Listing]:
    seen: set[str] = set()
    for page in range(query.max_pages):
        url = _build_url(query, commune, offset=page * PAGE_SIZE + 1)
        try:
            page_html = fetcher.get(url).text
        except HTTPError as error:
            if error.response.status_code == 404:  # paging past the last result
                return
            raise
        cards = HTMLParser(page_html).css("li.ui-search-layout__item")
        if not cards:
            return
        for card in cards:
            listing = _parse_card(card, commune)
            if listing and listing.external_id not in seen:
                seen.add(listing.external_id)
                yield listing


def _build_url(query: Query, commune: Commune | None, offset: int) -> str:
    path = [BASE, OPERATION_PATH[query.operation], "departamento"]
    path.append(f"{commune.value}-metropolitana" if commune else REGION)
    modifiers = ""
    if query.min_price is not None or query.max_price is not None:
        modifiers += f"/_PriceRange_{query.min_price or 0}-{query.max_price or '*'}CLP"
    if query.min_bedrooms is not None:
        modifiers += f"/_BEDROOMS_{query.min_bedrooms}-*"
    if offset > 1:
        modifiers += f"/_Desde_{offset}_NoIndex_True"
    return "/".join(path) + modifiers


def _parse_card(card: Node, commune: Commune | None) -> Listing | None:
    anchor = card.css_first("a.poly-component__title")
    price = _parse_price(card)
    if anchor is None or price is None:
        return None
    url = clean_url(anchor.attributes["href"])
    listing_id = LISTING_ID.search(url)
    if listing_id is None:
        return None

    amount, currency = price
    bedrooms, bathrooms, area_m2 = _parse_attributes(card)
    location = card.css_first(".poly-component__location")
    picture = card.css_first("img.poly-component__picture")
    return Listing(
        portal=NAME,
        external_id=listing_id.group(1),
        url=url,
        title=anchor.text(strip=True),
        price=amount,
        currency=currency,
        is_project=card.css_first(".poly-price__prefix") is not None,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        area_m2=area_m2,
        commune=commune.value if commune else None,
        address=location.text(strip=True) if location else None,
        image_url=_picture_url(picture),
    )


def clean_url(url: str) -> str:
    """Drop share/tracking parameters so one listing has one canonical URL."""
    return url.split("#")[0].split("?")[0]


def _picture_url(picture: Node | None) -> str | None:
    """Cards above the fold carry a real src; lazy ones keep it in data-src."""
    if picture is None:
        return None
    for attribute in ("src", "data-src"):
        url = picture.attributes.get(attribute) or ""
        if url.startswith("http"):
            return url
    return None


def _parse_price(card: Node) -> tuple[float, str] | None:
    symbol = card.css_first(".andes-money-amount__currency-symbol")
    fraction = card.css_first(".andes-money-amount__fraction")
    if symbol is None or fraction is None:
        return None
    amount = float(fraction.text(strip=True).replace(".", ""))
    cents = card.css_first(".andes-money-amount__cents")
    if cents is not None:
        amount += float(cents.text(strip=True)) / 100
    return amount, "UF" if symbol.text(strip=True) == "UF" else "CLP"


def _parse_attributes(card: Node) -> tuple[int | None, int | None, float | None]:
    """Read the card's attribute pills; ranges like '1 a 2 dormitorios' yield the lower bound."""
    bedrooms = bathrooms = area_m2 = None
    for pill in card.css(".poly-attributes_list__item"):
        text = pill.text(strip=True)
        value = _leading_number(text)
        if value is None:
            continue
        if "dormitorio" in text:
            bedrooms = int(value)
        elif "baño" in text:
            bathrooms = int(value)
        elif "m²" in text:
            area_m2 = value
    return bedrooms, bathrooms, area_m2


def _leading_number(text: str) -> float | None:
    match = LEADING_NUMBER.search(text)
    return float(match.group(1).replace(".", "")) if match else None


COORDS = re.compile(r"center=(-?\d+\.\d+)%2C(-?\d+\.\d+)")
PUBLISHED = re.compile(r"Publicado ([^<|]{3,30})")


def fetch_detail(fetcher: Fetcher, url: str) -> dict[str, object]:
    """Read one listing page: promoted spec columns, features JSON, coordinates and copy."""
    html = fetcher.get(url).text
    tree = HTMLParser(html)

    rows = [
        (cells[0].text(strip=True), cells[1].text(strip=True))
        for row in tree.css(".andes-table__row")
        if len(cells := row.css("th,td")) == 2
    ]
    detail = parse_specs(rows)

    coordinates = COORDS.search(html)
    if coordinates:
        detail["lat"] = float(coordinates.group(1))
        detail["lon"] = float(coordinates.group(2))

    description = tree.css_first(".ui-pdp-description__content")
    if description:
        detail["description"] = description.text(strip=True)

    published = PUBLISHED.search(tree.css_first(".ui-pdp-subtitle").text()) if tree.css_first(".ui-pdp-subtitle") else None
    if published:
        detail["published_label"] = published.group(1).strip()
        detail["published_days_ago"] = published_days_ago(published.group(1))

    transit = _parse_transit(tree)
    if transit:
        detail["transit"] = json.dumps(transit, ensure_ascii=False)
    if stations := transit.get("estaciones_de_metro"):
        nearest = min(stations, key=lambda station: station["metres"])
        detail |= {"nearest_station": nearest["name"], "station_distance_m": nearest["metres"],
                   "walk_minutes": nearest["minutes"], "walk_source": "portal"}

    broker = tree.css_first(".ui-vip-profile-info__info-container")
    if broker:
        detail["broker"] = broker.text(strip=True)

    detail |= _parse_price_benchmark(tree)
    return detail


POI_SUBTITLE = re.compile(r"(\d+)\s*mins?\s*-\s*([\d.,]+)\s*(metros|km)")
UF_PER_M2 = re.compile(r"([\d,]+)\s*UF/m")


def _parse_transit(tree: HTMLParser) -> dict[str, list[dict[str, object]]]:
    """Walking times the portal itself publishes, grouped by 'Estaciones de metro' / 'Paraderos'."""
    transit: dict[str, list[dict[str, object]]] = {}
    for subsection in tree.css(".ui-vip-poi__subsection"):
        title = subsection.css_first(".ui-vip-poi__subsection-title")
        if title is None:
            continue
        places = []
        for item in subsection.css(".ui-vip-poi__item"):
            name = item.css_first(".ui-vip-poi__item-title")
            subtitle = item.css_first(".ui-vip-poi__item-subtitle")
            match = POI_SUBTITLE.search(subtitle.text(strip=True)) if subtitle else None
            if name and match:
                metres = float(match.group(2).replace(".", "").replace(",", "."))
                places.append({
                    "name": name.text(strip=True),
                    "minutes": int(match.group(1)),
                    "metres": round(metres * 1000 if match.group(3) == "km" else metres),
                })
        if places:
            transit[_slug(title.text(strip=True))] = places
    return transit


def _parse_price_benchmark(tree: HTMLParser) -> dict[str, float]:
    """The portal's own UF/m² comparison of this listing against its zone."""
    comparison = tree.css_first(".ui-pdp-price-comparison")
    if comparison is None:
        return {}
    labels = [node.text(strip=True) for node in comparison.css("span") if node.text(strip=True)]
    benchmark = {}
    for label, column in (("Esta propiedad", "price_per_m2_uf"), ("Promedio en la zona", "zone_price_per_m2_uf")):
        if label in labels:
            match = UF_PER_M2.search(labels[labels.index(label) + 1])
            if match:
                benchmark[column] = float(match.group(1).replace(",", "."))
    return benchmark


PDP_PRICE = ".ui-pdp-price__second-line"


def fetch_standalone(fetcher: Fetcher, url: str) -> Listing | None:
    """Build a Listing from a detail page alone, for a link we have never scraped."""
    listing_id = LISTING_ID.search(url)
    if listing_id is None:
        return None

    tree = HTMLParser(fetcher.get(url).text)
    price_block = tree.css_first(PDP_PRICE)
    title = tree.css_first(".ui-pdp-title")
    if price_block is None or title is None:
        return None

    symbol = price_block.css_first(".andes-money-amount__currency-symbol")
    fraction = price_block.css_first(".andes-money-amount__fraction")
    if symbol is None or fraction is None:
        return None
    amount = float(fraction.text(strip=True).replace(".", ""))
    cents = price_block.css_first(".andes-money-amount__cents")
    if cents is not None:
        amount += float(cents.text(strip=True)) / 100

    return Listing(
        portal=NAME,
        external_id=listing_id.group(1),
        url=clean_url(url),
        title=title.text(strip=True),
        price=amount,
        currency="UF" if symbol.text(strip=True) == "UF" else "CLP",
    )
