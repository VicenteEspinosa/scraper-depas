"""Browsing the pool from the private chat: who may, what it shows, and what a press does."""
import pytest

from depas.browse import (
    CARD,
    DENIED,
    EMPTY,
    GO,
    LIST,
    ORDERS,
    POOL,
    PREFIX,
    PRIVATE_ONLY,
    RATE,
    SORT,
    STALE_KEYBOARD,
    STARRED,
    Where,
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


def _where(index=0, view=POOL, order=0, mode=CARD):
    """The four numbers every button carries, in the order the encoding puts them."""
    return f"{index}:{view}:{order}:{mode}"


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


def test_an_admin_opens_on_the_whole_pool_at_once(connection, posted):
    """The first question is what there is, not what the best one of them says."""
    open_browser(connection, _command(), Preferences.load(connection))

    text, keyboard = posted["sent"][0]
    assert "1-3 de 3" in text
    # One jump button per listing on the screen, so any line is one press from its card.
    assert _labels(keyboard, 0) == ["1", "2", "3"]


def test_the_card_screen_leads_with_the_best(connection, posted):
    """Read one at a time it is a ranked pool: the first card is the one worth seeing first."""
    text, keyboard = screen(connection, Preferences.from_env(), Where(mode=CARD))

    assert "1 de 3" in text
    assert _labels(keyboard, 0) == ["1/3", "▶️"]  # nothing before the first


def test_paging_moves_through_the_pool_in_one_message(connection, posted):
    """One message that edits itself, rather than a screenful of cards per browse."""
    _press(connection, f"{GO}:{_where(index=1)}")

    text, keyboard = posted["edited"][0]
    assert "2 de 3" in text
    assert _labels(keyboard, 0) == ["◀️", "2/3", "▶️"]


def test_the_last_screen_offers_no_way_further(connection, posted):
    """A button that cannot go anywhere is a button that lies about there being more."""
    _press(connection, f"{GO}:{_where(index=2)}")

    assert _labels(posted["edited"][0][1], 0) == ["◀️", "3/3"]


def test_an_index_past_the_end_lands_on_the_last(connection):
    """The pool shrinks under a keyboard: a stale index must not raise."""
    text, _ = screen(connection, Preferences.from_env(), Where(index=99))

    assert "3 de 3" in text


def test_a_star_from_the_browser_is_the_same_star(connection, posted):
    """One column, one meaning: a verdict given here is the verdict the card shows."""
    rated = _press(connection, f"{RATE}:{_where()}:1:{LIKE_BUTTON}")

    assert rated == 1
    assert connection.execute(
        "SELECT interest FROM listings WHERE rowid = 1").fetchone()["interest"] == LIKE
    assert posted["toasts"] == ["⭐ anotado"]


def test_a_discarded_listing_leaves_the_pool_it_was_browsed_in(connection, posted):
    """The pool is what the alerts draw from, and a dislike is out of it for good."""
    _press(connection, f"{RATE}:{_where()}:1:{DISLIKE_BUTTON}")

    assert "1 de 2" in posted["edited"][0][0]


def test_the_starred_view_shows_only_what_was_starred(connection, posted):
    """The shortlist is the other half of browsing: the same message, filtered."""
    set_interest(connection, "pi", "1", LIKE, "vicente")

    _press(connection, f"{GO}:{_where(view=STARRED)}")

    assert "1 de 1" in posted["edited"][0][0]


def test_an_empty_view_says_so_and_offers_no_keyboard(connection, posted):
    """Paging buttons over nothing would be three ways to stay where you are."""
    _press(connection, f"{GO}:{_where(view=STARRED)}")

    text, keyboard = posted["edited"][0]
    assert text == EMPTY[STARRED] and keyboard == {"inline_keyboard": []}


def test_every_button_fits_what_telegram_will_carry(connection, posted):
    """callback_data caps at 64 bytes, and past it Telegram drops the whole keyboard."""
    open_browser(connection, _command(), Preferences.load(connection))

    keyboard = posted["sent"][0][1]
    assert all(len(button["callback_data"].encode()) <= DATA_LIMIT
               for row in keyboard["inline_keyboard"] for button in row)


@pytest.mark.parametrize("data", ["g", "g:0", "g:0:0:0", "g:0:9:0:0", "g:0:0:99:0",
                                  "g:x:0:0:0", "z:0:0:0:0",
                                  f"{RATE}:0:0:0:0:1:nope"])
def test_a_keyboard_we_no_longer_speak_says_so(connection, posted, data):
    """A deploy can change the encoding under an open keyboard; a press must not traceback."""
    assert _press(connection, data) is None

    assert posted["toasts"] == [STALE_KEYBOARD]
    assert posted["edited"] == []


def test_a_press_is_authorised_every_time(connection, posted):
    """A private chat has one presser, but the whitelist can change between two presses."""
    assert _press(connection, f"{RATE}:{_where()}:1:{LIKE_BUTTON}", STRANGER) is None

    assert posted["toasts"] == [DENIED]
    assert connection.execute(
        "SELECT interest FROM listings WHERE rowid = 1").fetchone()["interest"] is None


def test_the_list_shows_a_screenful_and_a_button_into_each_line(connection, posted):
    """Ten at a time beats one at a time: the point of a list is comparing without paging."""
    text, keyboard = screen(connection, Preferences.from_env(), Where(mode=LIST))

    # The <pre> table is what makes the columns line up, so every listing is one row of it.
    assert text.count("Providencia") == 3
    assert _labels(keyboard, 0) == ["1", "2", "3"]


def test_a_line_of_the_list_opens_the_card_it_names(connection, posted):
    """Scanning is for choosing one; the jump is what turns the choice into a card."""
    jump = screen(connection, Preferences.from_env(),
                  Where(mode=LIST))[1]["inline_keyboard"][0][2]

    _press(connection, jump["callback_data"].removeprefix(PREFIX))

    assert "3 de 3" in posted["edited"][0][0]


def test_ordering_the_pool_reads_it_differently(connection, posted):
    """The same twenty flats are a different shortlist read cheapest-first than by grade."""
    cheapest = ORDERS.index(next(order for order in ORDERS if order.label == "precio"))

    text, _ = screen(connection, Preferences.from_env(), Where(mode=LIST, order=cheapest))

    prices = [line for line in text.splitlines() if "$" in line]
    assert prices[0].index("$550.000") and "por precio" in text


def test_re_ordering_starts_over(connection, posted):
    """Position 7 of one ranking is nowhere in the next, so the index cannot be kept."""
    keyboard = screen(connection, Preferences.from_env(), Where(index=2))[1]
    sort = next(button for row in keyboard["inline_keyboard"] for button in row
                if button["callback_data"].startswith(f"{PREFIX}{SORT}"))

    assert sort["callback_data"] == f"{PREFIX}{SORT}:0:{POOL}:1:{CARD}"


def test_a_listing_nobody_measured_sorts_last_rather_than_best(connection, posted):
    """Unknown is neither the smallest nor the biggest: it is unknown."""
    connection.execute("UPDATE listings SET area_m2 = NULL WHERE external_id = '0'")
    biggest = ORDERS.index(next(order for order in ORDERS if order.label == "metraje"))

    text, _ = screen(connection, Preferences.from_env(), Where(mode=LIST, order=biggest))

    unmeasured = next(line for line in text.splitlines() if "—" in line)
    assert unmeasured.strip().startswith("3.")
