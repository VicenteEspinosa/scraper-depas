"""Browsing the pool from the private chat: who may, what it shows, and what a press does."""
import pytest

from depas.browse import (
    DENIED,
    EMPTY,
    GO,
    POOL,
    PREFIX,
    PRIVATE_ONLY,
    RATE,
    STARRED,
    open_browser,
    press,
    screen,
)
from depas.configure import DATA_LIMIT
from depas.models import Listing
from depas.preferences import Preferences, set_preference
from depas.store import LIKE, connect, save, save_detail, set_interest
from depas.telegram import DISLIKE_BUTTON, LIKE_BUTTON

ADMIN = 467291452
STRANGER = 111111
PRIVATE, GROUP = 5, -1002


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    connection = connect(tmp_path / "test.db")
    set_preference(connection, "DEPAS_ADMINS", str(ADMIN))
    for index, price in enumerate((500_000, 700_000, 900_000)):
        save(connection, [Listing(portal="pi", external_id=str(index),
                                  url=f"https://x/{index}", price=price, currency="CLP",
                                  price_clp=price, area_m2=50.0, commune="providencia")])
        save_detail(connection, "pi", str(index), {"common_expenses": 50_000,
                                                   "walk_minutes": 5})
    return connection


@pytest.fixture
def posted(monkeypatch):
    """Everything the browser sends, edits and toasts, in the order it happens."""
    sent, edited, toasts = [], [], []
    monkeypatch.setattr("depas.browse.send_menu",
                        lambda chat, text, buttons, thread=None, reply_to=None:
                        sent.append((text, buttons)) or {"message_id": 1})
    monkeypatch.setattr("depas.browse.edit_menu",
                        lambda chat, message, text, buttons: edited.append((text, buttons)))
    monkeypatch.setattr("depas.browse.answer_callback",
                        lambda callback_id, text: toasts.append(text))
    return {"sent": sent, "edited": edited, "toasts": toasts}


def _command(chat_type="private", chat_id=PRIVATE, user_id=ADMIN):
    return {"chat": {"id": chat_id, "type": chat_type}, "message_id": 900,
            "from": {"id": user_id}, "text": "/top"}


def _press(connection, data, user_id=ADMIN):
    return press(connection, {"id": "1", "data": PREFIX + data, "from": {"id": user_id},
                              "message": {"chat": {"id": PRIVATE}, "message_id": 9}},
                 Preferences.load(connection))


def _labels(keyboard, row):
    return [button["text"] for button in keyboard["inline_keyboard"][row]]


def test_the_browser_is_for_the_private_chat(connection, posted):
    """In the group the pinned list already answers this, and a keyboard there is public."""
    open_browser(connection, _command("supergroup", GROUP), Preferences.load(connection))

    assert posted["sent"][0] == (PRIVATE_ONLY, None)


def test_a_stranger_is_told_their_own_id_rather_than_the_pool(connection, posted):
    """The same whitelist the settings use, and the same way into it."""
    open_browser(connection, _command(user_id=STRANGER), Preferences.load(connection))

    text, keyboard = posted["sent"][0]
    assert str(STRANGER) in text and keyboard is None


def test_an_admin_gets_the_best_listing_first(connection, posted):
    """It is a ranked pool: the first screen has to be the one worth seeing first."""
    open_browser(connection, _command(), Preferences.load(connection))

    text, keyboard = posted["sent"][0]
    assert "1 de 3" in text
    assert _labels(keyboard, 0) == ["1/3", "▶️"]  # nothing before the first


def test_paging_moves_through_the_pool_in_one_message(connection, posted):
    """One message that edits itself, rather than a screenful of cards per browse."""
    _press(connection, f"{GO}:1:{POOL}")

    text, keyboard = posted["edited"][0]
    assert "2 de 3" in text
    assert _labels(keyboard, 0) == ["◀️", "2/3", "▶️"]


def test_the_last_screen_offers_no_way_further(connection, posted):
    """A button that cannot go anywhere is a button that lies about there being more."""
    _press(connection, f"{GO}:2:{POOL}")

    assert _labels(posted["edited"][0][1], 0) == ["◀️", "3/3"]


def test_an_index_past_the_end_lands_on_the_last(connection):
    """The pool shrinks under a keyboard: a stale index must not raise."""
    text, _ = screen(connection, Preferences.from_env(), 99, POOL)

    assert "3 de 3" in text


def test_a_star_from_the_browser_is_the_same_star(connection, posted):
    """One column, one meaning: a verdict given here is the verdict the card shows."""
    rated = _press(connection, f"{RATE}:0:{POOL}:1:{LIKE_BUTTON}")

    assert rated == 1
    assert connection.execute(
        "SELECT interest FROM listings WHERE rowid = 1").fetchone()["interest"] == LIKE
    assert posted["toasts"] == ["⭐ anotado"]


def test_a_discarded_listing_leaves_the_pool_it_was_browsed_in(connection, posted):
    """The pool is what the alerts draw from, and a dislike is out of it for good."""
    _press(connection, f"{RATE}:0:{POOL}:1:{DISLIKE_BUTTON}")

    assert "1 de 2" in posted["edited"][0][0]


def test_the_starred_view_shows_only_what_was_starred(connection, posted):
    """The shortlist is the other half of browsing: the same message, filtered."""
    set_interest(connection, "pi", "1", LIKE, "vicente")

    _press(connection, f"{GO}:0:{STARRED}")

    assert "1 de 1" in posted["edited"][0][0]


def test_an_empty_view_says_so_and_offers_no_keyboard(connection, posted):
    """Paging buttons over nothing would be three ways to stay where you are."""
    _press(connection, f"{GO}:0:{STARRED}")

    text, keyboard = posted["edited"][0]
    assert text == EMPTY[STARRED] and keyboard == {"inline_keyboard": []}


def test_every_button_fits_what_telegram_will_carry(connection, posted):
    """callback_data caps at 64 bytes, and past it Telegram drops the whole keyboard."""
    open_browser(connection, _command(), Preferences.load(connection))

    keyboard = posted["sent"][0][1]
    assert all(len(button["callback_data"].encode()) <= DATA_LIMIT
               for row in keyboard["inline_keyboard"] for button in row)


def test_a_press_is_authorised_every_time(connection, posted):
    """A private chat has one presser, but the whitelist can change between two presses."""
    assert _press(connection, f"{RATE}:0:{POOL}:1:{LIKE_BUTTON}", STRANGER) is None

    assert posted["toasts"] == [DENIED]
    assert connection.execute(
        "SELECT interest FROM listings WHERE rowid = 1").fetchone()["interest"] is None
