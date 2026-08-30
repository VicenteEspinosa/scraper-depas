import pytest

from depas.communes import Commune
from depas.models import Query
from depas.portals import PORTALS, houm


def test_every_portal_exposes_the_same_interface():
    """The registry holds modules now, so each must carry the whole contract."""
    for name, module in PORTALS.items():
        assert module.NAME == name
        assert callable(module.search) and callable(module.fetch_detail)


@pytest.mark.parametrize(
    ("commune", "expected"),
    [(Commune.NUNOA, "Nunoa"), (Commune.LAS_CONDES, "Las Condes"),
     (Commune.ESTACION_CENTRAL, "Estacion Central"),
     (Commune.PEDRO_AGUIRRE_CERDA, "Pedro Aguirre Cerda")],
)
def test_commune_names_are_unaccented_and_title_cased(commune, expected):
    """Houm stores communes without accents, which the enum slugs already match."""
    assert houm.commune_name(commune) == expected


def test_a_result_becomes_a_listing():
    """The marketplace payload maps onto the shared Listing contract."""
    item = {
        "id": 178918, "address": "Avenida Rodrigo de Araya", "street_number": "1234",
        "comuna": "Ñuñoa",
        "price": [{"value": 400000.0, "currency": "CLP", "default": True},
                  {"value": 9.8, "currency": "CLF", "default": False}],
        "property_details": [{"dormitorios": 1, "banos": 1, "m_construidos": 42, "gc": 95000}],
        "photos": [{"url": "https://s3.amazonaws.com/x.jpg"}],
    }

    listing = houm._parse_result(item, Commune.NUNOA)

    assert (listing.portal, listing.external_id) == ("houm", "178918")
    assert (listing.price, listing.currency) == (400000.0, "CLP")
    assert (listing.bedrooms, listing.area_m2) == (1, 42)
    assert listing.url.endswith("/nunoa/178918")


def test_a_uf_only_listing_keeps_its_currency():
    """Without a CLP figure the CLF one is used and flagged as UF, not silently dropped."""
    item = {"id": 1, "comuna": "Ñuñoa", "price": [{"value": 30.5, "currency": "CLF"}],
            "property_details": [{}], "photos": []}

    listing = houm._parse_result(item, Commune.NUNOA)

    assert (listing.price, listing.currency) == (30.5, "UF")


def test_a_result_without_a_price_is_skipped():
    """A listing with no usable price cannot be graded, so it is not stored."""
    assert houm._parse_result({"id": 1, "comuna": "Ñuñoa", "price": [],
                               "property_details": [{}]}, Commune.NUNOA) is None


def test_pagination_stops_when_the_api_reports_no_next_page(monkeypatch):
    """The `next` field ends the walk rather than requesting empty pages."""
    pages = [{"results": [], "next": "more"}, {"results": [], "next": None}]
    calls = []

    class _Fetcher:
        def get(self, url, params=None):
            calls.append(params["page"])
            return type("R", (), {"json": lambda _: pages[len(calls) - 1]})()

    list(houm.search(_Fetcher(), Query(communes=[Commune.NUNOA], max_pages=5)))

    assert calls == ["1", "2"]
