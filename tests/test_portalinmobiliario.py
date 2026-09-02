import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import HTTPError
from selectolax.parser import HTMLParser

from depas.communes import Commune
from depas.detail import parse_specs, published_days_ago
from depas.metro import DETOUR_FACTOR, WALK_SPEED_M_PER_MIN, nearest_station
from depas.models import Query
from depas.portals.portalinmobiliario import _build_url, _parse_card, _parse_transit, search

FIXTURES = Path(__file__).parent / "fixtures"


def _card(name: str):
    return HTMLParser((FIXTURES / name).read_text()).css_first("li.ui-search-layout__item")


def test_parses_an_individual_unit():
    """A single-unit card yields exact price, attributes and a MLC id."""
    listing = _parse_card(_card("pi_unit_card.html"), Commune.LAS_CONDES)

    assert listing.external_id.startswith("MLC-")
    assert (listing.price, listing.currency) == (18.0, "UF")
    assert (listing.bedrooms, listing.bathrooms, listing.area_m2) == (1, 1, 32.0)
    assert listing.is_project is False


def test_project_card_keeps_the_lower_bound_of_each_range():
    """'1 a 2 dormitorios' / '79 - 81 m²' collapse to their minimum, and the card is flagged."""
    listing = _parse_card(_card("pi_project_card.html"), Commune.LAS_CONDES)

    assert listing.is_project is True
    assert (listing.bedrooms, listing.area_m2) == (2, 79.0)
    assert listing.currency == "UF"


@pytest.mark.parametrize(
    ("query", "offset", "expected"),
    [
        (Query(), 1, "/arriendo/departamento/nunoa-metropolitana"),
        (Query(operation="sale"), 1, "/venta/departamento/nunoa-metropolitana"),
        (Query(max_price=800_000), 1,
         "/arriendo/departamento/nunoa-metropolitana/_PriceRange_0-800000CLP"),
        (Query(min_bedrooms=2), 49,
         "/arriendo/departamento/nunoa-metropolitana/_BEDROOMS_2-*/"
         "_Desde_49_NoIndex_True"),
    ],
)
def test_build_url(query, offset, expected):
    """Filters and pagination render as path modifiers in the order the portal expects."""
    assert _build_url(query, Commune.NUNOA, offset).endswith(expected)


def test_build_url_without_a_commune_covers_the_whole_region():
    """No commune means the region-wide page, not a malformed path."""
    assert _build_url(Query(), None, 1).endswith("/arriendo/departamento/metropolitana")


class _FetcherStub:
    """Serves one page of cards, then 404s the way the portal does past the last result."""

    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.calls = 0

    def get(self, url: str):
        self.calls += 1
        if self.calls > self.pages:
            raise HTTPError("404", 0, SimpleNamespace(status_code=404))
        return SimpleNamespace(text=(FIXTURES / "pi_unit_card.html").read_text())


def test_pagination_stops_at_the_first_404_and_keeps_earlier_pages():
    """Paging past the last result 404s; that ends the commune without losing what was found."""
    fetcher = _FetcherStub(pages=2)

    listings = list(search(fetcher, Query(communes=[Commune.PROVIDENCIA], max_pages=5)))

    assert fetcher.calls == 3
    assert len(listings) == 1  # same card on both pages, deduped by external_id


@pytest.mark.parametrize(
    ("label", "days"),
    [("hace 39 días", 39), ("hace 3 meses", 90), ("hace 2 años", 730),
     ("esta semana", 3), ("por ", None)],
)
def test_published_days_ago(label, days):
    """Relative publication labels collapse to a comparable day count."""
    assert published_days_ago(label) == days


def test_specs_promote_known_fields_and_keep_the_rest_as_features():
    """Filterable rows become columns; unmapped rows survive as slugged JSON features."""
    parsed = parse_specs(
        [("Gastos comunes", "90.000 CLP"), ("Ascensor", "Sí"), ("Salón de usos múltiples", "Sí")]
    )

    assert (parsed["common_expenses"], parsed["has_elevator"]) == (90_000, 1)
    assert json.loads(parsed["features"]) == {"salon_de_usos_multiples": True}


def test_nearest_station_finds_the_station_at_its_own_coordinates():
    """A listing on top of El Golf resolves to El Golf at zero distance."""
    assert nearest_station(-33.41662, -70.59571) == ("El Golf", 0, 0)


def test_walk_minutes_scale_with_distance():
    """Walking time applies the detour factor to the straight-line distance."""
    _, metres, minutes = nearest_station(-33.4727258, -70.6191702)

    assert minutes == round(metres * DETOUR_FACTOR / WALK_SPEED_M_PER_MIN)


def test_portal_transit_beats_the_computed_estimate():
    """The portal's own routed walk times are parsed per section, metro and bus separately."""
    tree = HTMLParser((FIXTURES / "pi_poi_section.html").read_text())

    transit = _parse_transit(tree)

    assert [p["name"] for p in transit["estaciones_de_metro"]][:2] == ["Rodrigo de Araya", "Ñuble"]
    assert transit["estaciones_de_metro"][0] == {
        "name": "Rodrigo de Araya", "minutes": 12, "metres": 927}
    assert transit["paraderos"][0]["metres"] == 262


def test_kilometre_distances_are_normalised_to_metres():
    """A '1,1 km' subtitle becomes 1100 metres, not 1.1."""
    html = ('<div class="ui-vip-poi__subsection">'
            '<span class="ui-vip-poi__subsection-title">Estaciones de metro</span>'
            '<div class="ui-vip-poi__item"><div class="ui-vip-poi__item-title">Lejana</div>'
            '<div class="ui-vip-poi__item-subtitle">14 mins - 1,1 km</div></div></div>')

    transit = _parse_transit(HTMLParser(html))

    assert transit["estaciones_de_metro"][0]["metres"] == 1100


@pytest.mark.parametrize(("raw", "expected"), [("160 CLP", None), ("2 CLP", None),
                                               ("90.000 CLP", 90_000), ("10.000 CLP", 10_000)])
def test_implausibly_low_gastos_are_treated_as_undeclared(raw, expected):
    """A publisher typing 160 for 160.000 would silently understate the net cost."""
    parsed = parse_specs([("Gastos comunes", raw)])

    assert parsed.get("common_expenses") == expected
    if expected is None:
        assert json.loads(parsed["features"])["gastos_comunes"] == raw
