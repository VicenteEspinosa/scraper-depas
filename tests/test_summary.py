"""The pool at a glance: what /resumen says about the shape of what you are choosing between."""
import pytest

from depas.models import Listing
from depas.preferences import Preferences, set_preference
from depas.store import DISLIKE, LIKE, connect, save, save_detail, set_interest
from depas.summary import EMPTY, answer, format_summary

CHAT = -1002


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT))
    return connect(tmp_path / "test.db")


def _listing(connection, index, price, commune, enriched=True):
    save(connection, [Listing(portal="pi", external_id=str(index), url=f"https://x/{index}",
                              price=price, currency="CLP", price_clp=price, area_m2=50.0,
                              commune=commune)])
    if enriched:
        save_detail(connection, "pi", str(index),
                    {"common_expenses": 100_000, "walk_minutes": 5})


@pytest.fixture
def pool(connection):
    for index, (price, commune) in enumerate((
        (500_000, "providencia"), (600_000, "providencia"), (700_000, "nunoa"),
        (800_000, "nunoa"), (900_000, "las-condes"),
    )):
        _listing(connection, index, price, commune)
    return connection


def _summary(connection):
    return format_summary(connection, Preferences.load(connection))


def test_an_empty_pool_says_what_to_run(connection):
    """A blank screen and a screen saying `nothing yet` are not the same answer."""
    assert _summary(connection) == EMPTY


def test_the_pool_is_counted_and_its_grades_are_a_shape(pool):
    """A pool of C's must not be readable as a good week; the spread is what says which."""
    text = _summary(pool)

    assert "5 deptos" in text
    assert "█" in text  # every grade present is a bar, not a number to compare by eye


def test_the_band_is_what_the_pool_actually_spans(pool):
    """Cheapest, middle and dearest: the three figures a budget is decided against."""
    text = _summary(pool)

    assert "más barato · $600.000" in text
    assert "mediana · $800.000" in text
    assert "más caro · $1.000.000" in text


def test_the_budget_is_answered_in_listings(pool):
    """`9 de 22 bajo tu objetivo` is the only line that says whether the search is working."""
    set_preference(pool, "DEPAS_COST_TARGET", "800000")

    assert "3 de 5 bajo tu objetivo de $800.000" in _summary(pool)


def test_each_commune_is_a_row(pool):
    """Where to look is a decision the pool can answer and no single card can."""
    text = _summary(pool)

    assert "Providencia" in text and "Nunoa" in text and "Las Condes" in text


def test_one_commune_is_not_a_comparison(connection):
    """A table with a single row is the header restated."""
    _listing(connection, 0, 500_000, "providencia")

    assert "Por comuna" not in _summary(connection)


def test_the_verdicts_you_have_given_are_counted(pool):
    """What is left to judge is the question the summary exists to answer."""
    set_interest(pool, "pi", "0", LIKE, "vicente")
    set_interest(pool, "pi", "1", DISLIKE, "vicente")

    text = _summary(pool)

    assert "⭐ 1 marcado" in text and "🚫 1 descartado" in text


def test_a_discarded_listing_is_out_of_the_pool_it_is_counted_against(pool):
    """The same pool the alerts draw from: a dislike leaves it, and the count says so."""
    set_interest(pool, "pi", "0", DISLIKE, "vicente")

    assert "4 deptos" in _summary(pool)


def test_a_markdown_is_news_worth_a_line(pool):
    """The one thing that changed about a pool you have already read through."""
    _listing(pool, 0, 450_000, "providencia")

    assert "📉 1 bajó de precio" in _summary(pool)


def test_the_best_three_are_one_press_from_their_portal(pool):
    """A summary you cannot act on is a report; three links make it a place to start."""
    text = _summary(pool)

    assert text.count('<a href="https://x/') == 3


def test_an_unenriched_listing_is_not_in_the_pool_yet(connection):
    """Grading one on a search card alone would put a stranger at the top of the summary."""
    _listing(connection, 0, 500_000, "providencia", enriched=False)

    assert _summary(connection) == EMPTY


def test_it_is_answered_where_it_was_asked(pool, monkeypatch):
    """It carries no keyboard, so the group where the alerts land is a fine place for it."""
    answered = []
    monkeypatch.setattr("depas.summary.reply",
                        lambda chat, text, thread=None, reply_to=None:
                        answered.append((chat, thread, reply_to)))

    answer(pool, {"chat": {"id": CHAT, "type": "supergroup"}, "message_id": 7,
                  "message_thread_id": 3, "from": {"id": 1}}, Preferences.load(pool))

    assert answered == [(str(CHAT), 3, 7)]
