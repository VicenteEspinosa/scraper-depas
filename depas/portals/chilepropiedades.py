import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

from selectolax.parser import HTMLParser, Node

from depas.communes import Commune
from depas.detail import parse_specs
from depas.fetch import Fetcher
from depas.models import Currency, Listing, Query

NAME = "chilepropiedades"
BASE = "https://www.chilepropiedades.cl"
REGION = "region-metropolitana"
OPERATION_PATH = {"rent": "arriendo-mensual", "sale": "venta"}
CARD = "div.clp-list-search-card-layout"
LISTING_URL = re.compile(r"https?://(?:www\.)?chilepropiedades\.cl/ver-publicacion/\S+")
LISTING_ID = re.compile(r"/ver-publicacion/(?:[^/]+/)+(\d+)")
LD_JSON = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
NUMBER = re.compile(r"\d[\d.]*(?:,\d+)?")

CARD_CURRENCIES: dict[str, Currency] = {"$": "CLP", "UF": "UF"}
OFFER_CURRENCIES: dict[str, Currency] = {"CLP": "CLP", "CLF": "UF"}

# Chilepropiedades title-cases the spec labels Portal Inmobiliario writes in sentence case.
SPEC_LABELS = {
    "Gastos Comunes": "Gastos comunes",
    "Piso": "Número de piso de la unidad",
    "Superficie Útil": "Superficie útil",
}


def listing_id(url: str) -> str | None:
    match = LISTING_ID.search(url)
    return match.group(1) if match else None


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    for commune in query.communes or [None]:
        yield from _search_commune(fetcher, query, commune)


def _search_commune(fetcher: Fetcher, query: Query, commune: Commune | None) -> Iterator[Listing]:
    place = commune.value if commune is not None else REGION
    for page in range(query.max_pages):
        url = f"{BASE}/propiedades/{OPERATION_PATH[query.operation]}/departamento/{place}/{page}"
        cards = HTMLParser(fetcher.get(url).text).css(CARD)
        # paging past the last result answers 200 with no cards rather than 404
        if not cards:
            return
        for card in cards:
            listing = _parse_card(card)
            if listing is not None:
                yield listing


def _parse_card(card: Node) -> Listing | None:
    anchor = card.css_first("a.clp-listing-image-link")
    price = _parse_price(card)
    if anchor is None or price is None:
        return None
    url = BASE + anchor.attributes["href"]
    identifier = listing_id(url)
    if identifier is None:
        return None

    amount, currency = price
    features = _card_features(card)
    title = card.css_first("h2.publication-title-list")
    picture = card.css_first("picture img")
    return Listing(
        portal=NAME,
        external_id=identifier,
        url=url,
        title=title.text(strip=True) if title else None,
        price=amount,
        currency=currency,
        bedrooms=_count(features.get("Habitaciones:")),
        bathrooms=_count(features.get("Baños:")),
        area_m2=_number(features.get("Terreno:")),
        commune=commune_slug(url),
        image_url=BASE + picture.attributes["src"] if picture else None,
    )


def commune_slug(url: str) -> str:
    """A listing url ends in .../<commune>/departamento/<address>/<id>, and that slug is our own."""
    return url.split("/")[-4]


def _parse_price(card: Node) -> tuple[float, Currency] | None:
    """The displayed amount is rounded; the exact one sits in the `value` attribute."""
    symbol = card.css_first("a.clp-big-value span.clp-value-container")
    amount = card.css_first("a.clp-big-value span.clp-value-container[value]")
    if symbol is None or amount is None:
        return None
    currency = CARD_CURRENCIES.get(symbol.text(strip=True))
    return (float(amount.attributes["value"]), currency) if currency else None


def _card_features(card: Node) -> dict[str, str]:
    # strict=False: a markup change that unpairs the two leaves the specs it did pair
    # rather than killing the scrape.
    return {label.text(strip=True): value.text(strip=True)
            for label, value in zip(card.css("span.clp-feature-description"),
                                    card.css("span.clp-feature-value"), strict=False)}


def _number(text: str | None) -> float | None:
    """Card figures read like "49,95 m²", in Chilean number format."""
    match = NUMBER.search(text) if text else None
    return float(match.group().replace(".", "").replace(",", ".")) if match else None


def _count(text: str | None) -> int | None:
    number = _number(text)
    return int(number) if number else None


def fetch_standalone(fetcher: Fetcher, url: str) -> Listing | None:
    """Build a Listing from a detail page alone, for a link we have never scraped."""
    identifier = listing_id(url)
    published = _published_listing(HTMLParser(fetcher.get(url).text))
    if identifier is None or published is None:
        return None
    offer = published["offers"]
    return Listing(
        portal=NAME,
        external_id=identifier,
        url=published["url"],
        title=published["name"],
        price=float(offer["price"]),
        currency=OFFER_CURRENCIES[offer["priceCurrency"]],
        commune=commune_slug(published["url"]),
    )


# Chilepropiedades publishes no coordinates, so its listings never get a metro walk.
def fetch_detail(fetcher: Fetcher, url: str) -> dict[str, Any]:
    """Read the spec list the detail page renders, plus its schema.org listing block."""
    tree = HTMLParser(fetcher.get(url).text)
    specs = [(SPEC_LABELS.get(label, label), value) for label, value in _detail_specs(tree)]
    detail: dict[str, Any] = parse_specs(specs)

    published = _published_listing(tree)
    if published is not None:
        detail |= {
            "description": published["description"],
            "published_days_ago": _published_days_ago(published["datePosted"]),
            "broker": published["seller"]["name"],
        }
    return {key: value for key, value in detail.items() if value is not None}


def _detail_specs(tree: HTMLParser) -> list[tuple[str, str]]:
    return [(label.text(strip=True), value.text(strip=True))
            for label, value in zip(tree.css("span.clp-publication-detail-label"),
                                    tree.css("strong.clp-publication-detail-value"),
                                    strict=False)]


def _published_listing(tree: HTMLParser) -> dict[str, Any] | None:
    """The RealEstateListing node of the page's schema.org graph."""
    match = LD_JSON.search(tree.html or "")
    if match is None:
        return None
    graph = json.loads(match.group(1))["@graph"]
    return next((node for node in graph if node["@type"] == "RealEstateListing"), None)


def _published_days_ago(published_at: str) -> int:
    return (datetime.now(UTC).date() - date.fromisoformat(published_at)).days
