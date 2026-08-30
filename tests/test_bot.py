import pytest

from depas.bot import LISTING_LINK, _handle, _offset, _remember_offset
from depas.models import Listing
from depas.store import connect, save, save_detail


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    connection = connect(tmp_path / "test.db")
    save(connection, [Listing(portal="portalinmobiliario", external_id="MLC-1",
                              url="https://portalinmobiliario.com/MLC-1-x-_JM",
                              price=600_000, currency="CLP", price_clp=600_000, area_m2=50.0)])
    save_detail(connection, "portalinmobiliario", "MLC-1", {"common_expenses": 100_000})
    return connection


@pytest.fixture
def sent(monkeypatch):
    posted = []
    monkeypatch.setattr("depas.bot.send_listing",
                        lambda chat, text, image=None: posted.append((chat, text)))
    return posted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://portalinmobiliario.com/MLC-123-depto-_JM", 1),
        ("mira esto https://www.portalinmobiliario.com/MLC-9-x-_JM que tal", 1),
        ("dos: https://portalinmobiliario.com/MLC-1-a-_JM y https://portalinmobiliario.com/MLC-2-b-_JM", 2),
        ("https://www.houm.com/propiedad/123", 0),
        ("sin links", 0),
    ],
)
def test_link_detection(text, expected):
    """Only Portal Inmobiliario listing URLs are picked out of ordinary chat."""
    assert len(LISTING_LINK.findall(text)) == expected


def test_a_known_link_is_answered_without_refetching(connection, sent, monkeypatch):
    """A listing already in the database is graded from storage, with no HTTP call."""
    monkeypatch.setattr("depas.bot.portalinmobiliario.fetch_standalone",
                        lambda *a: pytest.fail("should not refetch a known listing"))
    monkeypatch.setattr("depas.bot.portalinmobiliario.fetch_detail",
                        lambda *a: pytest.fail("should not re-enrich"))

    _handle(connection, None, {"chat": {"id": -100},
                               "text": "https://portalinmobiliario.com/MLC-1-x-_JM"})

    assert len(sent) == 1
    assert sent[0][0] == "-100"


def test_the_same_link_twice_in_one_message_answers_once(connection, sent, monkeypatch):
    """Duplicate links in a single message must not produce duplicate replies."""
    url = "https://portalinmobiliario.com/MLC-1-x-_JM"

    _handle(connection, None, {"chat": {"id": -100}, "text": f"{url} y otra vez {url}"})

    assert len(sent) == 1


def test_a_message_with_no_link_is_ignored(connection, sent):
    """Ordinary group chatter produces no reply."""
    _handle(connection, None, {"chat": {"id": -100}, "text": "hola, alguien vio el depto?"})

    assert sent == []


def test_the_offset_survives_a_restart(connection):
    """Storing the offset means a restarted bot does not replay old updates."""
    assert _offset(connection) == 0

    _remember_offset(connection, 42)

    assert _offset(connection) == 42


@pytest.mark.parametrize(
    "url",
    [
        "https://departamento.mercadolibre.cl/MLC-4398180030?matt_tool=9#origin=share",
        "https://www.mercadolibre.cl/MLC-2-depto",
        "https://casa.mercadolibre.cl/MLC-3",
    ],
)
def test_mercadolibre_links_are_recognised(url):
    """Portal Inmobiliario is MercadoLibre's vertical, so either host is the same listing."""
    assert LISTING_LINK.search(url)


@pytest.mark.parametrize(
    "url",
    ["https://mercadolibre.com.ar/MLA-99", "https://www.houm.com/propiedad/123",
     "https://mercadolibre.cl/ofertas"],
)
def test_other_links_are_left_alone(url):
    """Another country's site, another portal, or a non-listing page must not trigger a reply."""
    assert not LISTING_LINK.search(url)


def test_the_same_listing_on_either_host_is_one_row(connection, sent, monkeypatch):
    """A shared MercadoLibre link for a listing scraped from Portal Inmobiliario is not refetched."""
    monkeypatch.setattr("depas.bot.portalinmobiliario.fetch_standalone",
                        lambda *a: pytest.fail("should recognise the id, not refetch"))
    monkeypatch.setattr("depas.bot.portalinmobiliario.fetch_detail",
                        lambda *a: pytest.fail("should not re-enrich"))

    _handle(connection, None, {"chat": {"id": -100},
                               "text": "https://departamento.mercadolibre.cl/MLC-1?ua=x"})

    assert len(sent) == 1


def test_tracking_parameters_are_stripped(connection, sent, monkeypatch):
    """Share links carry tracking junk that must not become part of the stored URL."""
    from depas.portals.portalinmobiliario import clean_url

    assert clean_url("https://departamento.mercadolibre.cl/MLC-4?matt_tool=9&ua=z#origin=share") \
        == "https://departamento.mercadolibre.cl/MLC-4"
