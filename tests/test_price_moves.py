"""What a listing's price did: read back off price_history, and said on every surface."""
import pytest

from depas.grade import Scale
from depas.models import Listing
from depas.preferences import Preferences
from depas.shortlist import format_shortlist
from depas.store import LIKE, connect, save, save_detail, set_interest
from depas.telegram import format_listing, price_change
from tests.support import prefs


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1002")
    connection = connect(tmp_path / "test.db")
    _seen(connection, 700_000)
    save_detail(connection, "pi", "1", {"walk_minutes": 5, "common_expenses": 100_000})
    return connection


def _seen(connection, price, currency="CLP"):
    """One scrape pass finding the listing at this price, which is what records a move."""
    save(connection, [Listing(portal="pi", external_id="1", url="https://x/1", price=price,
                              currency=currency,
                              price_clp=price if currency == "CLP" else price * 39_000,
                              area_m2=50.0, commune="providencia")])


def _row(connection):
    return dict(connection.execute("SELECT * FROM listings_ranked").fetchone())


def test_a_listing_that_never_moved_says_nothing(connection):
    """Every listing has a price_history row from the day it was found; that is not a move."""
    assert price_change(_row(connection)) is None


def test_a_markdown_is_read_back_off_the_history(connection):
    """The portals record it and never show it: a flat marked down looks like any other."""
    _seen(connection, 630_000)

    share, was, changed_at = price_change(_row(connection))

    assert round(share, 2) == -0.1
    assert (was, bool(changed_at)) == (700_000, True)


def test_the_card_names_the_old_figure(connection):
    """A move you cannot check against the price it moved from is a claim, not a fact."""
    _seen(connection, 630_000)
    row = _row(connection)

    card = format_listing(row, Scale(prefs()).grade(row), prefs())

    assert "📉" in card and "bajó $70.000" in card and "antes $700.000" in card


def test_a_rise_is_shown_too(connection):
    """A landlord who put the rent up is exactly as worth knowing about."""
    _seen(connection, 770_000)
    row = _row(connection)

    assert "📈" in format_listing(row, Scale(prefs()).grade(row), prefs())


def test_rounding_is_not_a_price_move(connection):
    """Portals restate their own prices; under half a percent that is noise, not news."""
    _seen(connection, 701_000)

    assert price_change(_row(connection)) is None


def test_a_re_publication_in_another_currency_is_not_a_discount(connection):
    """A listing that moved from pesos to UF is a different figure, not a markdown."""
    _seen(connection, 18.0, currency="UF")

    assert price_change(_row(connection)) is None


def test_the_last_move_is_the_one_shown(connection):
    """Two markdowns are one story: what it costs now against what it cost before that."""
    _seen(connection, 660_000)
    _seen(connection, 630_000)

    share, was, _ = price_change(_row(connection))

    assert (was, round(share, 3)) == (660_000, round(630_000 / 660_000 - 1, 3))


def test_the_pinned_list_flags_what_moved(connection):
    """A flat you starred that has just been marked down is the one to call about today."""
    _seen(connection, 630_000)
    set_interest(connection, "pi", "1", LIKE, "vicente")

    listed = format_shortlist(connection, Preferences.load(connection), "-1002")

    assert "📉 -10%" in listed
