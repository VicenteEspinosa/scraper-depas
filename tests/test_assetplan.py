from types import SimpleNamespace

import pytest

from depas.communes import Commune
from depas.models import Query
from depas.portals import assetplan
from depas.portals.assetplan import fetch_detail, fetch_standalone, listing_id, search, unit_url

SEARCH_PAGE = (
    '<meta name="csrf-token" content="tok3n">'
    "<div x-data=\"{ ...buildingMapSearch([-70.6483, -33.4569], 12, "
    "{ autoSearch: false, limit: 5000 }, 'nunoa', 190) }\"></div>"
)

# Assetplan owns this building outright, so the search result lists its units inline.
OWNED_BUILDING = {
    "id": 3632, "nombre": "Guillermo Mann", "direccion": "Av. Vicuña Mackenna 2362",
    "comuna": "Ñuñoa", "commune_slug": "nunoa", "building_id": 3632, "multifamily": True,
    "min_price": 411000, "max_price": 580000, "total_available_count": 84,
    "photo_urls": ["https://d23gr057zjkjxx.cloudfront.net/mann.jpg"],
    "available_units": [
        {"id": 523942, "bedrooms": 1, "bathrooms": 1, "price": 411000, "m2_utiles": 33,
         "size_category": "1-dormitorio", "has_discount": True, "discount_percentage": 50},
        {"id": 523940, "bedrooms": 2, "bathrooms": 2, "price": 580000, "m2_utiles": 55,
         "size_category": "2-dormitorios", "has_discount": False, "discount_percentage": 0},
    ],
}

# A unit Assetplan only manages arrives as a result of its own, keyed by building and unit.
MANAGED_UNIT = {
    "id": "2328_535413", "nombre": "Casa Bustamante", "direccion": "Av. Gral. Bustamante 1007",
    "comuna": "Ñuñoa", "commune_slug": "nunoa", "building_id": 2328, "unit_id": 535413,
    "multifamily": False, "price": 510000, "bedrooms": 1, "bathrooms": 1, "m2_utiles": 34,
    "photo_urls": ["https://d23gr057zjkjxx.cloudfront.net/bustamante.jpg"],
}

UNIT = {
    "id": 523940, "unidad": "206-B", "piso": 2, "orientacion": "SO",
    "m2_utiles": "32", "m2_terraza": "3", "acepta_mascotas": True, "furnished": 0,
    "has_parking": False, "has_store": True, "ggcc_final": "77000.00",
    "typology": {"id": 1, "bedrooms": 1, "bathrooms": 1},
    "price": {"monto_depto": "412000.00", "gc_depto": "77000.00",
              "porcentaje_descuento": "50.00", "precio_recomendado": "380000.00"},
    "price_with_discount": 206000,
    "building_id": 3632, "building_name": "Edificio Guillermo Mann", "building_commune": "Ñuñoa",
    "building_photo": "https://d23gr057zjkjxx.cloudfront.net/mann.jpg",
    "building": {"nombre": "Edificio Guillermo Mann", "comuna": "Ñuñoa",
                 "direccion": "Av. Vicuña Mackenna 2362"},
}


class _SearchFetcherStub:
    def __init__(self, *buildings: dict) -> None:
        self.buildings = list(buildings)
        self.posted: dict = {}
        self.headers: dict = {}

    def get(self, url: str):
        return SimpleNamespace(text=SEARCH_PAGE)

    def post(self, url: str, json: dict, headers: dict):
        self.posted, self.headers = json, headers
        return SimpleNamespace(json=lambda: {"success": True,
                                             "data": {"buildings": self.buildings}})


class _UnitFetcherStub:
    def __init__(self, unit: dict) -> None:
        self.unit = unit
        self.urls: list[str] = []

    def get(self, url: str, headers: dict | None = None):
        self.urls.append(url)
        coordinates = {"data": {"_geo": {"lat": -33.471947, "lng": -70.623199}}}
        return SimpleNamespace(json=lambda: self.unit if "/units/" in url else coordinates)


def _search(*buildings: dict) -> list:
    return list(search(_SearchFetcherStub(*buildings), Query(communes=[Commune.NUNOA])))


def test_an_owned_building_yields_one_listing_per_available_unit():
    """The result is a building, but each unit it lists is priced and sized on its own."""
    listings = _search(OWNED_BUILDING)

    assert [listing.external_id for listing in listings] == ["523942", "523940"]
    assert [(listing.price, listing.area_m2, listing.bedrooms) for listing in listings] == [
        (411000.0, 33, 1), (580000.0, 55, 2)]
    assert listings[0].commune == "nunoa"
    assert listings[0].address == "Av. Vicuña Mackenna 2362"
    assert not listings[0].is_project


def test_a_managed_unit_is_keyed_by_its_own_unit_id():
    """That result carries no unit list because it already is one unit."""
    listing = _search(MANAGED_UNIT)[0]

    assert (listing.external_id, listing.price, listing.area_m2) == ("535413", 510000.0, 34)
    assert listing.url.endswith("/casa-bustamante/2328/retail/535413")


def test_a_unit_without_a_price_is_skipped():
    """A listing with no usable price cannot be graded, so it is not stored."""
    unpriced = {**OWNED_BUILDING, "available_units": [{"id": 1, "bedrooms": 1, "bathrooms": 1}]}

    assert _search(unpriced) == []


def test_the_search_reuses_the_page_token_and_commune_id():
    """The API only answers a POST carrying the CSRF token and commune id the page rendered."""
    fetcher = _SearchFetcherStub(OWNED_BUILDING)

    list(search(fetcher, Query(communes=[Commune.NUNOA])))

    assert fetcher.posted == {"commune_id": 190}
    assert fetcher.headers["X-CSRF-TOKEN"] == "tok3n"


@pytest.mark.parametrize(
    ("operation", "communes"),
    [("sale", [Commune.NUNOA]), ("rent", [])],
)
def test_a_query_assetplan_cannot_serve_is_reported_not_silently_empty(operation, communes):
    """Assetplan publishes rentals, one commune at a time; the CLI reports the rest as skipped."""
    with pytest.raises(NotImplementedError):
        list(search(_SearchFetcherStub(), Query(operation=operation, communes=communes)))


@pytest.mark.parametrize(
    ("url", "expected"),
    [("https://www.assetplan.cl/arriendo/departamento/nunoa/guillermo-mann/3632/retail/523940",
      "523940"),
     ("https://www.assetplan.cl/arriendo/departamento/nunoa", None)],
)
def test_a_listing_url_yields_its_id(url, expected):
    """The bot recognises pasted links by the unit id closing the path."""
    assert listing_id(url) == expected


def test_a_built_url_round_trips_to_the_unit_it_names():
    """Accents and punctuation in a building name must not break the link back to the unit."""
    url = unit_url("nunoa", "Edificio Ñuñoa (Etapa 2)", 3632, 523940)

    assert url.endswith("/nunoa/edificio-nunoa-etapa-2/3632/retail/523940")
    assert listing_id(url) == "523940"


def test_the_unit_endpoint_fills_the_shared_spec_columns(monkeypatch):
    """One unit's specs plus the building coordinates the metro walk is computed from."""
    monkeypatch.setattr(assetplan, "uf_in_clp", lambda fetcher: 40_000.0)
    fetcher = _UnitFetcherStub(UNIT)

    detail = fetch_detail(fetcher, "https://www.assetplan.cl/a/b/3632/retail/523940")

    assert (detail["area_useful_m2"], detail["terrace_m2"], detail["has_terrace"]) == (32.0, 3.0, 1)
    assert (detail["floor"], detail["orientation"]) == (2, "SurOriente")
    assert (detail["common_expenses"], detail["pets_allowed"]) == (77000, 1)
    assert (detail["parking_spaces"], detail["storage_units"]) == (0, 1)
    assert (detail["lat"], detail["lon"]) == (-33.471947, -70.623199)
    assert fetcher.urls[0].endswith("/units/523940")


@pytest.mark.parametrize("ggcc", ["0.00", 0])
def test_undeclared_common_expenses_read_as_missing(monkeypatch, ggcc):
    """Assetplan quotes gastos comunes as a string or a number, and zero means undeclared."""
    monkeypatch.setattr(assetplan, "uf_in_clp", lambda fetcher: 40_000.0)

    detail = fetch_detail(_UnitFetcherStub({**UNIT, "ggcc_final": ggcc}),
                          "https://www.assetplan.cl/a/b/3632/retail/523940")

    assert "common_expenses" not in detail


def test_the_recommended_rent_becomes_the_zone_benchmark(monkeypatch):
    """Grading needs both figures in UF per m2, and Assetplan quotes only pesos."""
    monkeypatch.setattr(assetplan, "uf_in_clp", lambda fetcher: 40_000.0)

    detail = fetch_detail(_UnitFetcherStub(UNIT),
                          "https://www.assetplan.cl/a/b/3632/retail/523940")

    assert detail["price_per_m2_uf"] == 0.3219
    assert detail["zone_price_per_m2_uf"] == 0.2969


def test_a_standalone_link_is_priced_at_the_standing_rent():
    """The headline price is a one-month promotion; the lease is signed at monto_depto."""
    listing = fetch_standalone(_UnitFetcherStub(UNIT),
                               "https://www.assetplan.cl/a/b/3632/retail/523940")

    assert (listing.price, listing.currency) == (412000.0, "CLP")
    assert (listing.bedrooms, listing.bathrooms, listing.area_m2) == (1, 1, 32.0)
    assert listing.commune == "nunoa"
    assert listing.url.endswith("/nunoa/edificio-guillermo-mann/3632/retail/523940")
