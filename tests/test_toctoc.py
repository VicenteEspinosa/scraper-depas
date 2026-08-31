import pytest

from depas.portals import toctoc

CARD = {
    "titulo": "Zañartu 1315 Depto 1 Dorm Piso 15 con Terraza",
    "comuna": "Ñuñoa",
    "hashId": "00a97b7018a25247c3b357e49c621fc6fc1be78a",
    "urlFicha": "https://www.toctoc.com/arriendo/departamento/metropolitana/nunoa/"
                "b_00a97b7018a25247c3b357e49c621fc6fc1be78a",
    "imagenPrincipal": {"src": "https://d1cfu8v5n1wsm.cloudfront.net/x.jpg"},
    "precios": [{"order": 0, "prefix": "UF", "value": "10"},
                {"order": 1, "prefix": "$", "value": "396.100"}],
    "superficie": ["42"], "dormitorios": ["1"], "bannos": ["1"],
}


def test_a_card_becomes_a_listing():
    """The list page payload maps onto the shared Listing contract."""
    listing = toctoc._parse_result(CARD)

    assert (listing.portal, listing.external_id) == ("toctoc", CARD["hashId"])
    assert (listing.price, listing.currency) == (396100.0, "CLP")
    assert (listing.bedrooms, listing.bathrooms, listing.area_m2) == (1, 1, 42)
    assert (listing.commune, listing.image_url) == ("nunoa", "https://d1cfu8v5n1wsm.cloudfront.net/x.jpg")


def test_the_clp_price_wins_over_the_rounded_uf_one():
    """TocToc rounds UF to whole units, so the peso figure is the accurate one."""
    assert toctoc._price(CARD["precios"]) == (396100.0, "CLP")


def test_a_uf_only_card_keeps_its_currency():
    """Without a peso figure the UF one is used and flagged as UF, not silently dropped."""
    assert toctoc._price([{"prefix": "UF", "value": "1.250"}]) == (1250.0, "UF")


def test_a_card_without_a_price_is_skipped():
    """A listing with no usable price cannot be graded, so it is not stored."""
    assert toctoc._parse_result({**CARD, "precios": []}) is None


@pytest.mark.parametrize(("values", "expected"),
                         [(["42"], 42.0), (["42,76"], 42.76), (["0"], None), ([], None)])
def test_undeclared_card_figures_read_as_missing(values, expected):
    """TocToc writes "0" where a publisher left the figure out, and decimals with a comma."""
    assert toctoc._number(values) == expected


def test_a_listing_url_yields_its_id():
    """The bot recognises pasted links by the hash in the last path segment."""
    assert toctoc.listing_id(CARD["urlFicha"]) == CARD["hashId"]
    assert toctoc.listing_id("https://www.toctoc.com/arriendo/departamento/nunoa") is None


def test_abbreviated_spec_labels_land_in_the_shared_columns():
    """TocToc abbreviates the surface labels Portal Inmobiliario spells out."""
    property_data = {"characteristics": [
        {"name": "Dormitorios:", "value": "1 "},
        {"name": "Superf. útil: ", "value": "32 m²"},
        {"name": "Superf. terraza: ", "value": "3 m²"},
        {"name": "Estado del proyecto: ", "value": "Entrega inmediata"},
    ]}

    specs = [(toctoc.SPEC_LABELS.get(label, label), value)
             for label, value in toctoc._characteristics(property_data)]

    assert dict(specs) == {"Dormitorios": "1", "Superficie útil": "32 m²",
                           "Superficie de terraza": "3 m²",
                           "Disponible desde": "Entrega inmediata"}
