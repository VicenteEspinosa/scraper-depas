from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import RequestException

from depas.bot import (GONE, NO_CARD, _handle, _handle_callback, _offset, _remember_offset,
                       find_links, run)
from depas.models import Listing
from depas.store import POOL_QUERY, connect, remember_card, save, save_detail


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

    def send(chat, text, image=None, thread=None, buttons=None):
        posted.append((chat, text, thread))
        # Telegram's own record of the message, which is what the bot stores.
        return {"chat": {"id": int(chat)}, "message_id": 500 + len(posted)}

    monkeypatch.setattr("depas.bot.send_listing", send)
    return posted


@pytest.fixture
def answers(monkeypatch):
    """Every plain reply the bot posts, every card it redraws, every keyboard it ticks."""
    said, edited, ticked = [], [], []
    monkeypatch.setattr("depas.bot.reply",
                        lambda chat, text, thread=None, reply_to=None: said.append(text))
    monkeypatch.setattr("depas.bot.edit_listing",
                        lambda chat, message, text, is_photo=False, buttons=None:
                        edited.append((chat, message, text, buttons)))
    monkeypatch.setattr("depas.bot.edit_buttons",
                        lambda chat, message, buttons: ticked.append((chat, message, buttons)))
    return SimpleNamespace(said=said, edited=edited, ticked=ticked)


@pytest.fixture
def offered(monkeypatch):
    """Every verdict keyboard the bot posts into a card's comment thread."""
    posted = []

    def send(chat, text, thread, buttons):
        posted.append((chat, thread, buttons))
        return {"chat": {"id": int(chat)}, "message_id": KEYBOARD}

    monkeypatch.setattr("depas.bot.send_buttons", send)
    return posted


@pytest.fixture
def pressed(monkeypatch):
    """Every toast the bot answers a pressed button with."""
    toasts = []
    monkeypatch.setattr("depas.bot.answer_callback",
                        lambda callback_id, text: toasts.append(text))
    return toasts


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
    assert len(find_links(text)) == expected


def test_a_known_link_is_answered_without_refetching(connection, sent, monkeypatch):
    """A listing already in the database is graded from storage, with no HTTP call."""
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_standalone",
                        lambda *a: pytest.fail("should not refetch a known listing"))
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail",
                        lambda *a: pytest.fail("should not re-enrich"))

    _handle(connection, None, {"chat": {"id": -100},
                               "text": "https://portalinmobiliario.com/MLC-1-x-_JM"})

    assert len(sent) == 1
    assert sent[0][0] == "-100"


def test_a_comment_is_answered_inside_its_own_thread(connection, sent):
    """A link pasted under a channel post is graded in that post's comments, not the group."""
    _handle(connection, None, {"chat": {"id": -100}, "message_thread_id": 12,
                               "text": "https://portalinmobiliario.com/MLC-1-x-_JM"})

    assert sent[0][2] == 12


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
    assert find_links(url)


@pytest.mark.parametrize(
    "url",
    ["https://mercadolibre.com.ar/MLA-99", "https://www.houm.com/propiedad/123",
     "https://mercadolibre.cl/ofertas"],
)
def test_other_links_are_left_alone(url):
    """Another country's site, another portal, or a non-listing page must not trigger a reply."""
    assert not find_links(url)


def test_the_same_listing_on_either_host_is_one_row(connection, sent, monkeypatch):
    """A shared MercadoLibre link for a listing scraped from Portal Inmobiliario is not refetched."""
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_standalone",
                        lambda *a: pytest.fail("should recognise the id, not refetch"))
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail",
                        lambda *a: pytest.fail("should not re-enrich"))

    _handle(connection, None, {"chat": {"id": -100},
                               "text": "https://departamento.mercadolibre.cl/MLC-1?ua=x"})

    assert len(sent) == 1


def test_tracking_parameters_are_stripped(connection, sent, monkeypatch):
    """Share links carry tracking junk that must not become part of the stored URL."""
    from depas.portals.portalinmobiliario import clean_url

    assert clean_url("https://departamento.mercadolibre.cl/MLC-4?matt_tool=9&ua=z#origin=share") \
        == "https://departamento.mercadolibre.cl/MLC-4"


def test_a_link_never_scraped_still_gets_its_net_cost(connection, sent, monkeypatch):
    """Pasting an unseen listing must price it in CLP, so net cost and the comparison appear."""
    monkeypatch.setenv("DEPAS_CURRENT_COST", "710000")
    monkeypatch.setattr("depas.uf.uf_in_clp", lambda fetcher: 40_000.0)
    monkeypatch.setattr(
        "depas.portals.portalinmobiliario.fetch_standalone",
        lambda fetcher, url: Listing(portal="portalinmobiliario", external_id="MLC-7",
                                     url=url, price=20.0, currency="UF"),
    )
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail",
                        lambda fetcher, url: {"common_expenses": 150_000, "area_useful_m2": 66.0})

    _handle(connection, None, {"chat": {"id": -100},
                               "text": "https://departamento.mercadolibre.cl/MLC-7"})

    assert "💰 <b>$950.000</b> neto al mes" in sent[0][1]
    assert "⚖️ 🔺 $240.000 más caro que hoy" in sent[0][1]

@pytest.mark.parametrize(
    ("text", "portal"),
    [
        ("https://portalinmobiliario.com/MLC-1-x-_JM", "portalinmobiliario"),
        ("https://departamento.mercadolibre.cl/MLC-2?ua=x", "portalinmobiliario"),
        ("https://houm.com/cl/arriendo-departamento-region-metropolitana/nunoa/178918", "houm"),
    ],
)
def test_links_are_attributed_to_the_right_portal(text, portal):
    """The bot must know which portal a pasted link belongs to before fetching it."""
    assert find_links(text)[0][0] == portal


def test_a_houm_page_that_is_not_a_listing_is_ignored():
    """Marketing pages on a supported host must not be mistaken for listings."""
    assert find_links("https://houm.com/cl/propietario/arriendo") == []



CHANNEL, CARD, GROUP, THREAD, KEYBOARD = -1001, 77, -1002, 88, 950


@pytest.fixture
def announced(connection, offered):
    """A card posted to the channel and copied by Telegram into its discussion group."""
    remember_card(connection, CHANNEL, CARD, "portalinmobiliario", "MLC-1")
    _handle(connection, None, {
        "chat": {"id": GROUP}, "message_id": THREAD, "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "chat": {"id": CHANNEL}, "message_id": CARD},
        "text": "🟢 B 80 ✔️",
    })
    return connection


def _comment(text, **extra):
    """A comment left in the card's thread, as Telegram delivers it."""
    return {"chat": {"id": GROUP}, "message_id": 900, "message_thread_id": THREAD,
            "from": {"username": "vicente"}, "text": text, **extra}


def _verdict(connection):
    return connection.execute(
        "SELECT interest, rated_by FROM listings WHERE external_id = 'MLC-1'").fetchone()


def test_a_like_in_the_thread_marks_that_apartment(announced, answers):
    """The thread a comment sits in is what says which listing the command is about."""
    _handle(announced, None, _comment("/like"))

    assert tuple(_verdict(announced)) == (1, "vicente")
    assert answers.said == ["⭐ anotado como interesante"]


def test_a_dislike_takes_the_listing_out_of_the_pool(announced, answers):
    """Turning a listing down has to stop it being announced and stop it skewing the ranking."""
    _handle(announced, None, _comment("/dislike"))

    assert _verdict(announced)["interest"] == -1
    assert announced.execute(POOL_QUERY).fetchall() == []


def test_the_card_itself_is_redrawn_with_the_verdict(announced, answers):
    """The mark belongs on the card, so the channel is scannable without opening threads."""
    _handle(announced, None, _comment("/like"))

    chat, message, text, _ = answers.edited[0]
    assert (chat, message) == (str(CHANNEL), CARD)
    assert text.startswith("⭐ ")


def test_the_command_is_recognised_when_addressed_to_the_bot(announced, answers):
    """Telegram appends @thebot whenever more than one bot shares the chat."""
    _handle(announced, None, _comment("/dislike@depas_bot"))

    assert _verdict(announced)["interest"] == -1


def test_a_command_with_no_card_behind_it_says_so(connection, answers):
    """A command shouted into the group rates nothing rather than rating the wrong thing."""
    _handle(connection, None, {"chat": {"id": GROUP}, "message_id": 900, "text": "/like"})

    assert _verdict(connection)["interest"] is None
    assert answers.said == [NO_CARD]


def test_a_reply_to_a_card_the_bot_posted_is_enough(connection, sent, answers):
    """In a plain group there are no threads: the card is whatever the command answers."""
    _handle(connection, None, {"chat": {"id": GROUP}, "message_id": 1,
                               "text": "https://portalinmobiliario.com/MLC-1-x-_JM"})

    _handle(connection, None, {"chat": {"id": GROUP}, "message_id": 900, "text": "/like",
                               "reply_to_message": {"message_id": 501}})

    assert _verdict(connection)["interest"] == 1


def test_an_older_card_is_traced_by_the_id_it_prints(connection, answers):
    """Cards posted before the bot recorded them still carry [id] in their header."""
    listing_id = connection.execute(
        "SELECT id FROM listings_ranked WHERE external_id = 'MLC-1'").fetchone()["id"]

    _handle(connection, None, {
        "chat": {"id": GROUP}, "message_id": 900, "text": "/dislike",
        "reply_to_message": {"message_id": 4, "text": f"🟢 B 80 ✔️ · Ñuñoa · [{listing_id}]"},
    })

    assert _verdict(connection)["interest"] == -1
    # Nothing to edit: that card was posted before its ids were being kept.
    assert answers.edited == []


def test_the_channels_own_copy_is_never_answered(connection, sent):
    """Telegram copies each card into the discussion group; replying would post it twice."""
    _handle(connection, None, {
        "chat": {"id": GROUP}, "message_id": THREAD, "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "chat": {"id": CHANNEL}, "message_id": CARD},
        "text": "🟢 B 80 https://portalinmobiliario.com/MLC-1-x-_JM",
    })

    assert sent == []


def test_a_card_too_old_to_edit_still_keeps_the_verdict(announced, answers, monkeypatch):
    """Telegram refuses edits past 48 hours; the rating is the part that matters."""
    def refuses(*args, **kwargs):
        raise RuntimeError("telegram editMessageText failed: message can't be edited")

    monkeypatch.setattr("depas.bot.edit_listing", refuses)

    _handle(announced, None, _comment("/like"))

    assert _verdict(announced)["interest"] == 1
    assert answers.said == ["⭐ anotado como interesante"]


def _press(connection, data, chat=CHANNEL, message_id=CARD):
    """A button press, as Telegram delivers it: the card it sat on, and what it carries."""
    return {"id": "cb-1", "data": data, "from": {"username": "vicente"},
            "message": {"chat": {"id": chat}, "message_id": message_id}}


def _listing_id(connection):
    return connection.execute(
        "SELECT id FROM listings_ranked WHERE external_id = 'MLC-1'").fetchone()["id"]


def test_a_pressed_button_records_the_verdict(announced, answers, pressed):
    """The whole point of the buttons: a verdict with nothing typed."""
    _handle_callback(announced, _press(announced, f"like:{_listing_id(announced)}"))

    assert tuple(_verdict(announced)) == (1, "vicente")
    assert pressed == ["⭐ anotado como interesante"]
    # A toast, not a message: pressing a button must not fill the thread with replies.
    assert answers.said == []


def test_a_pressed_button_redraws_the_card_it_sat_on(announced, answers, pressed):
    """The card has to show the new verdict, and keep its buttons — an edit drops them."""
    _handle_callback(announced, _press(announced, f"dislike:{_listing_id(announced)}"))

    chat, message, text, buttons = answers.edited[0]
    assert (chat, message) == (str(CHANNEL), CARD)
    assert text.startswith("🚫 ")
    assert buttons["inline_keyboard"][0][1]["text"] == "🚫 Descartado ✓"


def test_pressing_the_copy_in_the_group_edits_the_channel_post(announced, answers, pressed):
    """The discussion group's copy belongs to the channel; the post behind it is ours to edit."""
    _handle_callback(announced, _press(announced, f"like:{_listing_id(announced)}",
                                       chat=GROUP, message_id=THREAD))

    chat, message, _, _ = answers.edited[0]
    assert (chat, message) == (str(CHANNEL), CARD)


def test_the_thread_opens_with_the_verdict_keyboard(announced, offered):
    """A channel card cannot carry the buttons without hiding its own comments button,
    so they are posted as the first comment in the thread instead."""
    chat, thread, buttons = offered[0]

    assert (chat, thread) == (str(GROUP), THREAD)
    assert [button["text"] for button in buttons["inline_keyboard"][0]] \
        == ["⭐ Me interesa", "🚫 Descartar"]


def test_a_forward_of_something_we_never_posted_gets_no_keyboard(connection, offered):
    """Every channel post is copied into the group; only our cards have a verdict."""
    _handle(connection, None, {
        "chat": {"id": GROUP}, "message_id": 91, "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "chat": {"id": CHANNEL}, "message_id": 12},
        "text": "aviso a mano",
    })

    assert offered == []


def test_a_keyboard_that_fails_to_post_still_leaves_the_thread_linked(connection, monkeypatch,
                                                                     answers):
    """The link is what the typed commands need; the keyboard is only the shortcut."""
    def refuses(*args, **kwargs):
        raise RuntimeError("telegram sendMessage failed: not enough rights")

    monkeypatch.setattr("depas.bot.send_buttons", refuses)
    remember_card(connection, CHANNEL, CARD, "portalinmobiliario", "MLC-1")
    _handle(connection, None, {
        "chat": {"id": GROUP}, "message_id": THREAD, "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "chat": {"id": CHANNEL}, "message_id": CARD},
    })

    _handle(connection, None, _comment("/like"))

    assert _verdict(connection)["interest"] == 1


def test_the_keyboard_in_the_thread_rates_the_card_above_it(announced, answers, pressed):
    """The press lands on a comment, not on the card: the thread says which card it is."""
    _handle_callback(announced, {
        "id": "cb-1", "data": f"like:{_listing_id(announced)}", "from": {"username": "vicente"},
        "message": {"chat": {"id": GROUP}, "message_id": KEYBOARD, "message_thread_id": THREAD},
    })

    assert _verdict(announced)["interest"] == 1
    chat, message, _, _ = answers.edited[0]
    assert (chat, message) == (str(CHANNEL), CARD)


def test_the_pressed_keyboard_is_ticked_where_it_sits(announced, answers, pressed):
    """The buttons live on their own message, so the tick has to be sent there."""
    _handle_callback(announced, {
        "id": "cb-1", "data": f"dislike:{_listing_id(announced)}", "from": {"username": "v"},
        "message": {"chat": {"id": GROUP}, "message_id": KEYBOARD, "message_thread_id": THREAD},
    })

    chat, message, buttons = answers.ticked[0]
    assert (chat, message) == (str(GROUP), KEYBOARD)
    assert buttons["inline_keyboard"][0][1]["text"] == "🚫 Descartado ✓"


def test_a_press_on_the_card_itself_is_not_ticked_twice(announced, answers, pressed):
    """The redraw of a card already carries its keyboard; a second edit would be noise."""
    _handle_callback(announced, _press(announced, f"like:{_listing_id(announced)}"))

    assert answers.ticked == []


def test_a_button_for_a_listing_that_is_gone_is_answered_anyway(announced, answers, pressed):
    """An unanswered press spins in the client until it times out, so every path answers."""
    _handle_callback(announced, _press(announced, "like:9999"))

    assert pressed == [GONE]
    assert answers.edited == []


def test_a_button_press_is_dispatched_by_the_poll_loop(poll, monkeypatch):
    """Presses ride the same getUpdates poll as messages — there is no second listener."""
    handled = []
    monkeypatch.setattr("depas.bot._handle_callback",
                        lambda connection, callback: handled.append(callback["data"]))

    poll([{"update_id": 7, "callback_query": {"id": "cb-1", "data": "like:1"}}], StopLoop())

    assert handled == ["like:1"]


def test_a_new_card_carries_the_buttons(connection, monkeypatch):
    """A card posted with no keyboard would leave nothing to press."""
    posted = []
    monkeypatch.setattr("depas.bot.send_listing",
                        lambda chat, text, image=None, thread=None, buttons=None:
                        posted.append(buttons) or {"chat": {"id": -100}, "message_id": 1})

    _handle(connection, None, {"chat": {"id": -100}, "message_id": 1,
                               "text": "https://portalinmobiliario.com/MLC-1-x-_JM"})

    labels = [button["text"] for button in posted[0]["inline_keyboard"][0]]
    assert labels == ["⭐ Me interesa", "🚫 Descartar"]


class StopLoop(Exception):
    """Sentinel that ends the bot's endless poll loop once a test has seen enough."""


@pytest.fixture
def poll(connection, monkeypatch):
    """Drive run() over a script of getUpdates outcomes; an Exception instance is raised."""
    def drive(*outcomes):
        scripted = iter(outcomes)
        polls = []

        def getUpdates(method, **params):
            polls.append(params)
            outcome = next(scripted)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr("depas.bot.call", getUpdates)
        monkeypatch.setattr("depas.bot.connect", lambda *args, **kwargs: connection)
        monkeypatch.setattr("depas.bot.Fetcher", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("depas.bot.stored_uf", lambda *args: None)
        monkeypatch.setattr("depas.bot.time.sleep", lambda _: None)
        with pytest.raises(StopLoop):
            run()
        return polls
    return drive


def test_a_telegram_blip_costs_one_poll_not_the_process(poll):
    """A failed getUpdates backs off and polls again rather than ending the bot."""
    polls = poll(RuntimeError("Conflict: terminated by other getUpdates request"), StopLoop())

    assert len(polls) == 2


def test_a_dropped_connection_is_survived_too(poll):
    """The network failing mid-poll is the same kind of blip as Telegram saying no."""
    polls = poll(RequestException("connection reset"), StopLoop())

    assert len(polls) == 2


def _portal_is_down(*args: object) -> None:
    raise RuntimeError("portal is down")


def test_an_update_that_cannot_be_answered_is_not_redelivered(poll, monkeypatch):
    """A reply that fails still advances the offset, or every restart retries it forever."""
    advanced: list[int] = []
    monkeypatch.setattr("depas.bot._handle", _portal_is_down)
    monkeypatch.setattr("depas.bot._remember_offset",
                        lambda connection, offset: advanced.append(offset))

    poll([{"update_id": 41, "message": {"chat": {"id": 1}, "text": "hola"}}], StopLoop())

    assert advanced == [42]
