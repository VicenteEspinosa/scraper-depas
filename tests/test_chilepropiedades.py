from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from selectolax.parser import HTMLParser

from depas.communes import Commune
from depas.detail import parse_specs
from depas.models import Query
from depas.portals.chilepropiedades import (
    SPEC_LABELS,
    _detail_specs,
    _number,
    _parse_card,
    _published_days_ago,
    _published_listing,
    fetch_detail,
    listing_id,
    search,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _card(name: str):
    return HTMLParser((FIXTURES / name).read_text()).css_first("div.clp-list-search-card-layout")


def test_a_card_becomes_a_listing():
    """A search card carries everything the shared Listing contract needs."""
    listing = _parse_card(_card("cp_unit_card.html"))

    assert (listing.portal, listing.external_id) == ("chilepropiedades", "124607880")
    assert (listing.price, listing.currency) == (680000.0, "CLP")
    assert (listing.bedrooms, listing.bathrooms, listing.area_m2) == (2, 1, 47.0)
    assert listing.commune == "nunoa"


def test_a_uf_card_keeps_the_unrounded_amount():
    """The card displays "UF 15" but publishes 15.19 in the value attribute."""
    listing = _parse_card(_card("cp_uf_card.html"))

    assert (listing.price, listing.currency) == (15.19, "UF")


def test_a_card_without_a_price_is_skipped():
    """A listing with no usable price cannot be graded, so it is not stored."""
    card = _card("cp_unit_card.html")
    for span in card.css("a.clp-big-value span.clp-value-container"):
        span.decompose()

    assert _parse_card(card) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("62 m²", 62.0), ("49,95 m²", 49.95), ("1.450.000", 1450000.0), ("", None), (None, None)],
)
def test_chilean_number_formats_are_read_correctly(text, expected):
    """Thousands use dots and decimals a comma, so plain float() would misread both."""
    assert _number(text) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [("https://www.chilepropiedades.cl/ver-publicacion/arriendo-mensual/nunoa/"
      "departamento/jose-domingo-canas/83729367", "83729367"),
     ("https://www.chilepropiedades.cl/propiedades/arriendo-mensual/departamento/nunoa/0", None)],
)
def test_a_listing_url_yields_its_id(url, expected):
    """The bot recognises pasted links by the numeric id closing the path."""
    assert listing_id(url) == expected


class _FetcherStub:
    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        body = (FIXTURES / "cp_unit_card.html").read_text() if len(self.urls) <= self.pages else ""
        return SimpleNamespace(text=body)


def test_pagination_stops_at_the_first_empty_page():
    """Paging past the last result answers 200 with no cards rather than 404."""
    fetcher = _FetcherStub(pages=2)

    listings = list(search(fetcher, Query(communes=[Commune.NUNOA], max_pages=5)))

    assert len(fetcher.urls) == 3
    assert len(listings) == 2
    assert fetcher.urls[0].endswith("/arriendo-mensual/departamento/nunoa/0")


def test_a_query_without_communes_searches_the_whole_region():
    """The portal has no commune-less url, so a region-wide search names the region."""
    fetcher = _FetcherStub(pages=0)

    list(search(fetcher, Query(operation="sale")))

    assert fetcher.urls == ["https://www.chilepropiedades.cl/propiedades/venta/"
                            "departamento/region-metropolitana/0"]


def test_detail_labels_land_in_the_shared_columns():
    """Chilepropiedades title-cases labels that parse_specs knows in sentence case."""
    tree = HTMLParser((FIXTURES / "cp_detail.html").read_text())

    specs = parse_specs([(SPEC_LABELS.get(label, label), value)
                         for label, value in _detail_specs(tree)])

    assert specs["common_expenses"] == 120000
    assert specs["floor"] == 5
    assert specs["area_useful_m2"] == 45.0


def test_the_detail_page_supplies_the_broker_and_description():
    """The schema.org block carries what the spec list leaves out."""
    fetcher = SimpleNamespace(get=lambda url: SimpleNamespace(
        text=(FIXTURES / "cp_detail.html").read_text()))

    detail = fetch_detail(fetcher, "https://www.chilepropiedades.cl/ver-publicacion/x/y/z/1")

    assert detail["broker"] == "Andes Trust propiedades"
    assert detail["description"].startswith("El departamento")


def test_publication_dates_become_a_day_count():
    """Grading compares recency across portals, which all report it in days."""
    posted = (datetime.now(UTC).date() - timedelta(days=5)).isoformat()

    assert _published_days_ago(posted) == 5


def test_a_page_without_a_schema_block_still_yields_its_specs():
    """A detail page missing its ld+json must not lose the spec list beside it."""
    tree = HTMLParser("<div><span class='clp-publication-detail-label'>Piso</span>"
                      "<strong class='clp-publication-detail-value'>7</strong></div>")

    assert _published_listing(tree) is None
    assert _detail_specs(tree) == [("Piso", "7")]
