import argparse

import pytest

from depas.cli import _announce
from depas.models import Listing
from depas.store import connect, save, save_detail
from depas.telegram import format_listing


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    connection = connect(tmp_path / "test.db")
    for index in range(4):
        save(connection, [Listing(portal="pi", external_id=str(index), url=f"https://x/{index}",
                                  price=500_000 + index * 50_000, currency="CLP",
                                  price_clp=500_000 + index * 50_000, area_m2=50.0)])
        save_detail(connection, "pi", str(index), {"walk_minutes": index + 1, "has_elevator": 1})
    return connection


@pytest.fixture
def sent(monkeypatch):
    posted = []
    monkeypatch.setattr("depas.cli.send_listing",
                        lambda chat, text, image=None: posted.append((text, image)))
    monkeypatch.setattr("depas.cli.time.sleep", lambda _: None)  # no real rate-limit wait
    return posted


def test_each_listing_is_announced_only_once(connection, sent):
    """A second pass posts nothing new, however often the watch runs."""
    first = _announce(connection, limit=10)
    second = _announce(connection, limit=10)

    assert (first, second) == (4, 0)
    assert len(sent) == 4


def test_the_limit_caps_one_pass_without_losing_the_rest(connection, sent):
    """Capping a run leaves the remainder for the next pass rather than dropping it."""
    assert _announce(connection, limit=2) == 2

    assert _announce(connection, limit=10) == 2


def test_listings_below_the_minimum_grade_are_never_reconsidered(connection, sent, monkeypatch):
    """Sub-threshold listings are stamped, so they cannot resurface as the pool shifts."""
    monkeypatch.setenv("DEPAS_ALERT_MIN_GRADE", "90")

    posted = _announce(connection, limit=10)

    assert posted < 4
    assert connection.execute(
        "SELECT COUNT(*) FROM listings WHERE notified_at IS NULL"
    ).fetchone()[0] == 0


def test_the_card_escapes_html_and_keeps_the_link(connection):
    """A title with markup must not break Telegram's HTML parse mode."""
    from depas.grade import Scale

    row = {"commune": "nunoa", "bedrooms": 2, "area": 50.0, "net_monthly_clp": 600_000,
           "price_clp": 500_000, "common_expenses": 100_000, "url": "https://x/1?a=1&b=2",
           "nearest_station": "Ñuble <test>", "walk_minutes": 5}

    card = format_listing(row, Scale([row]).grade(row))

    assert "&lt;test&gt;" in card and "<test>" not in card
    assert 'href="https://x/1?a=1&amp;b=2"' in card


def test_the_card_shows_the_publication_title():
    """The listing's own title appears, escaped, under the grade line."""
    from depas.grade import Scale

    row = {"commune": "nunoa", "title": "Depto 2D & luminoso", "area": 50.0,
           "net_monthly_clp": 600_000, "price_clp": 500_000, "url": "https://x/1"}

    card = format_listing(row, Scale([row]).grade(row))

    assert "<i>Depto 2D &amp; luminoso</i>" in card
    assert "arriendo + gastos comunes" in card  # no amount published, and never a dash
    assert "—" not in card


def test_a_listing_with_a_photo_is_sent_as_one(monkeypatch):
    """sendPhoto carries the card as a caption; without an image it falls back to text."""
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.call",
                        lambda method, **params: calls.append(method) or {})

    from depas.telegram import send_listing

    send_listing("-100", "card", "https://img/1.webp")
    send_listing("-100", "card", None)

    assert calls == ["sendPhoto", "sendMessage"]
