"""The ⭐ set as one pinned message: what it says, and when it is rewritten."""
import re
from types import SimpleNamespace

import pytest

from depas import shortlist
from depas.bot import _handle
from depas.models import Listing
from depas.shortlist import EMPTY, LIMIT, MOST, format_shortlist, sync
from depas.store import LIKE, connect, remember_card, save, save_detail, set_interest
from depas.telegram import message_link
from tests.support import prefs

CHANNEL, CARD, GROUP, THREAD = -1001, 77, -1002, 88


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHANNEL))
    return connect(tmp_path / "test.db")


def _listing(connection, external_id="MLC-1", price=600_000, area=50.0):
    save(connection, [Listing(portal="portalinmobiliario", external_id=external_id,
                              url=f"https://portalinmobiliario.com/{external_id}-x-_JM",
                              price=price, currency="CLP", price_clp=price, area_m2=area,
                              commune="providencia")])
    save_detail(connection, "portalinmobiliario", external_id, {"common_expenses": 100_000})
    return external_id


@pytest.fixture
def telegram(monkeypatch):
    """Every message the list posts, edits and pins, with Telegram's own replies."""
    posted, edited, pinned = [], [], []

    def post(chat, text, thread=None, reply_to=None):
        posted.append((chat, text))
        return {"chat": {"id": int(chat)}, "message_id": 300 + len(posted)}

    monkeypatch.setattr("depas.shortlist.reply", post)
    monkeypatch.setattr("depas.shortlist.edit_text",
                        lambda chat, message, text: edited.append((chat, message, text)))
    monkeypatch.setattr("depas.shortlist.pin",
                        lambda chat, message: pinned.append((chat, message)))
    return SimpleNamespace(posted=posted, edited=edited, pinned=pinned)


def test_a_list_with_nothing_in_it_says_how_to_fill_it(connection):
    """A pinned message that is simply blank teaches nobody what the star is for."""
    assert format_shortlist(connection, prefs(), str(CHANNEL)) == EMPTY


def test_a_starred_listing_is_named_priced_and_graded(connection):
    """The point of the list is deciding between them, so each line carries the figures."""
    external_id = _listing(connection)
    set_interest(connection, "portalinmobiliario", external_id, LIKE, "vicente")

    text = format_shortlist(connection, prefs(), str(CHANNEL))

    assert "Providencia" in text and "$700.000" in text and "50 m²" in text


def test_an_entry_links_back_to_the_card_it_was_announced_on(connection):
    """The card is where the buttons and the breakdown are: the list is a way back to it."""
    external_id = _listing(connection)
    remember_card(connection, CHANNEL, CARD, "portalinmobiliario", external_id)
    set_interest(connection, "portalinmobiliario", external_id, LIKE, "vicente")

    text = format_shortlist(connection, prefs(), str(CHANNEL))

    assert message_link(CHANNEL, CARD) in text
    assert "tarjeta" in text and "aviso" in text


def test_a_listing_that_was_never_announced_still_has_its_link(connection):
    """A link pasted into the chat has no card of ours; the portal is the way back."""
    external_id = _listing(connection)
    set_interest(connection, "portalinmobiliario", external_id, LIKE, "vicente")

    text = format_shortlist(connection, prefs(), str(CHANNEL))

    assert "tarjeta" not in text
    assert f"https://portalinmobiliario.com/{external_id}-x-_JM" in text


def test_the_list_is_ordered_best_first(connection):
    """It is read top down, so what it puts first has to be what is worth reading first."""
    for external_id, price in (("cheap", 400_000), ("dear", 900_000)):
        _listing(connection, external_id, price)
        set_interest(connection, "portalinmobiliario", external_id, LIKE, "vicente")

    text = format_shortlist(connection, prefs(), str(CHANNEL))

    assert text.index("cheap") < text.index("dear")


def test_a_list_too_long_to_post_says_how_much_it_left_out(connection):
    """Telegram rejects a message past its limit rather than trimming it, so the trim
    is ours to do -- and every listing left out is still accounted for."""
    total = MOST + 3
    for index in range(total):
        external_id = _listing(connection, f"MLC-{index}", 400_000 + index * 1_000)
        set_interest(connection, "portalinmobiliario", external_id, LIKE, "vicente")

    text = format_shortlist(connection, prefs(), str(CHANNEL))

    assert len(text) <= LIMIT
    shown = text.count("<code>[")
    assert shown + int(re.search(r"…y (\d+) más", text).group(1)) == total


def test_the_list_is_posted_and_pinned_the_first_time(connection, telegram):
    """Nothing pins it for you, and an unpinned list is one more thing that scrolls away."""
    assert sync(connection, prefs()) is True

    assert len(telegram.posted) == 1
    assert telegram.pinned == [(str(CHANNEL), 301)]


def test_the_same_message_is_rewritten_from_then_on(connection, telegram):
    """One list, always current: a second message would be a second answer to the question."""
    sync(connection, prefs())
    _listing(connection)
    set_interest(connection, "portalinmobiliario", "MLC-1", LIKE, "vicente")

    sync(connection, prefs())

    assert len(telegram.posted) == 1
    assert telegram.edited[-1][:2] == (str(CHANNEL), 301)


def test_a_list_that_cannot_be_pinned_is_still_kept(connection, telegram, monkeypatch):
    """Pinning needs rights the bot may not have; the list is worth having either way."""
    def refuses(chat, message):
        raise RuntimeError("telegram pinChatMessage failed: not enough rights")

    monkeypatch.setattr("depas.shortlist.pin", refuses)

    assert sync(connection, prefs()) is True
    sync(connection, prefs())

    assert len(telegram.posted) == 1  # remembered despite the pin, so it is edited next


def test_nothing_about_the_list_can_cost_a_verdict(connection, monkeypatch):
    """It is a convenience; the verdict is the thing that actually had to be recorded."""
    def refuses(*args, **kwargs):
        raise RuntimeError("telegram sendMessage failed: chat not found")

    monkeypatch.setattr("depas.shortlist.reply", refuses)

    assert sync(connection, prefs()) is False


def test_a_verdict_in_the_chat_rewrites_the_list(connection, telegram, monkeypatch):
    """The list is only ever right if every verdict is what updates it."""
    external_id = _listing(connection)
    remember_card(connection, GROUP, THREAD, "portalinmobiliario", external_id)
    monkeypatch.setattr("depas.bot.reply", lambda *args, **kwargs: None)
    monkeypatch.setattr("depas.bot.edit_listing", lambda *args, **kwargs: None)
    monkeypatch.setattr("depas.bot.edit_text", lambda *args, **kwargs: None)

    _handle(connection, None, {"chat": {"id": GROUP}, "message_id": 900,
                               "from": {"username": "vicente"}, "text": "/like",
                               "reply_to_message": {"message_id": THREAD}}, prefs())

    assert telegram.posted and str(shortlist.MOST) not in telegram.posted[0][1]
    assert "Providencia" in telegram.posted[0][1]
