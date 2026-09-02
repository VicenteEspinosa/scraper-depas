"""The settings menu: what it offers, who it obeys, and what a press actually writes."""
import json

import pytest

from depas import configure
from depas.configure import (DATA_LIMIT, GROUPS, KIND, LABELS, LAST_ADMIN, MENU, PREFIX,
                             answer_prompt, group_screen, main_screen, open_menu, press,
                             setting_screen)
from depas.preferences import BY_NAME, Preferences, set_preference
from depas.store import connect

ADMIN = 467291452
STRANGER = 111111


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    connection = connect(tmp_path / "test.db")
    set_preference(connection, "DEPAS_ADMINS", str(ADMIN))
    return connection


@pytest.fixture
def posted(monkeypatch):
    """Everything the menu sends, edits, asks for and toasts, in the order it happens."""
    sent, edited, asked, toasts = [], [], [], []
    monkeypatch.setattr("depas.configure.send_menu",
                        lambda chat, text, buttons, thread=None, reply_to=None:
                        sent.append((text, buttons)) or {"message_id": 1})
    monkeypatch.setattr("depas.configure.edit_menu",
                        lambda chat, message, text, buttons: edited.append((text, buttons)))
    monkeypatch.setattr("depas.configure.ask_value",
                        lambda chat, text, thread=None: asked.append(text) or {"message_id": 2})
    monkeypatch.setattr("depas.configure.answer_callback",
                        lambda callback_id, text: toasts.append(text))
    return {"sent": sent, "edited": edited, "asked": asked, "toasts": toasts}


def _press(connection, data, user_id=ADMIN):
    press(connection, {"id": "1", "data": PREFIX + data, "from": {"id": user_id},
                       "message": {"chat": {"id": 5}, "message_id": 9}},
          Preferences.load(connection))


def _message(text, user_id=ADMIN, replying=None):
    message = {"chat": {"id": 5}, "message_id": 7, "text": text, "from": {"id": user_id}}
    if replying is not None:
        message["reply_to_message"] = {"text": replying}
    return message


def _labels(keyboard):
    return [button["text"] for row in keyboard["inline_keyboard"] for button in row]


def _data_for(keyboard, label):
    for row in keyboard["inline_keyboard"]:
        for button in row:
            if button["text"] == label:
                return button["callback_data"].removeprefix(PREFIX)
    raise AssertionError(f"no button labelled {label!r} in {_labels(keyboard)}")


# -- the menu covers the registry ------------------------------------------------


def test_every_setting_is_reachable_from_the_menu():
    """A knob nobody can navigate to is a knob the chat does not configure."""
    shown = {name for _, _, names in MENU for name in names}

    assert shown == set(BY_NAME)


def test_every_setting_has_a_label_and_a_kind_of_keyboard():
    """Adding a Setting has to be all it takes, so anything missing fails here."""
    for name, declared in BY_NAME.items():
        assert name in LABELS, f"{name} has no label"
        assert declared.parse.__name__ in KIND, f"{name} has no keyboard for its parser"


def test_no_button_outgrows_what_telegram_will_carry(connection):
    """Telegram drops a whole keyboard past 64 bytes of callback_data, silently."""
    screens = [main_screen()] + [group_screen(key, Preferences.load(connection))
                                 for key in GROUPS]
    prefs = Preferences.load(connection)
    screens += [setting_screen(connection, name, prefs) for name in BY_NAME]
    for _, keyboard in screens:
        for row in keyboard["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= DATA_LIMIT, button


# -- who the menu obeys ----------------------------------------------------------


def test_a_stranger_gets_their_id_rather_than_the_menu(connection, posted):
    """The bootstrap: an id is what somebody pastes into DEPAS_ADMINS to become one."""
    open_menu(connection, _message("/config", user_id=STRANGER), Preferences.load(connection))

    text, keyboard = posted["sent"][0]
    assert str(STRANGER) in text
    assert keyboard is None


def test_a_channel_post_has_nobody_to_authorise(connection, posted):
    """A post is signed by the channel, so there is no person the whitelist could match."""
    open_menu(connection, {"chat": {"id": 5}, "message_id": 7, "text": "/config"},
              Preferences.load(connection))

    assert "no hay a quién autorizar" in posted["sent"][0][0]


def test_a_stranger_pressing_a_button_changes_nothing(connection, posted):
    """The menu is a message: in a group anybody can reach somebody else's buttons."""
    _press(connection, "v:COST_TARGET:123456", user_id=STRANGER)

    assert posted["toasts"] == [configure.DENIED]
    assert Preferences.load(connection).value("DEPAS_COST_TARGET") != 123456
    assert posted["edited"] == []


def test_a_stranger_cannot_answer_a_prompt_either(connection, posted):
    """The typed path writes too, so it carries the same check as the buttons."""
    handled = answer_prompt(connection, None, _message("999", user_id=STRANGER,
                                                       replying="⚙️ DEPAS_ADMINS · agregar"),
                            Preferences.load(connection))

    assert handled
    assert Preferences.load(connection).admins() == [ADMIN]


# -- what a press writes ---------------------------------------------------------


def test_a_stepper_writes_the_value_it_shows(connection, posted):
    """A button carries the number it would land on, not a delta: a stale press is
    predictable rather than compounding."""
    set_preference(connection, "DEPAS_COST_TARGET", "800000")
    _, keyboard = setting_screen(connection, "DEPAS_COST_TARGET", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "+$25.000"))

    assert Preferences.load(connection).value("DEPAS_COST_TARGET") == 825_000


def test_a_stepper_never_walks_a_setting_below_zero(connection, posted):
    set_preference(connection, "DEPAS_WALK_TARGET", "2")
    _, keyboard = setting_screen(connection, "DEPAS_WALK_TARGET", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "−5"))

    assert Preferences.load(connection).value("DEPAS_WALK_TARGET") == 0


def test_a_weight_is_six_presets_and_never_needs_typing(connection, posted):
    _, keyboard = setting_screen(connection, "DEPAS_COST_WEIGHT", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "2"))

    assert Preferences.load(connection).weights()["cost"] == 2.0


def test_the_weight_in_use_is_ticked(connection):
    set_preference(connection, "DEPAS_METRO_WEIGHT", "1.5")

    _, keyboard = setting_screen(connection, "DEPAS_METRO_WEIGHT", Preferences.load(connection))

    assert "● 1,5" in _labels(keyboard)


def test_editing_the_sublet_income_from_the_chat_moves_the_ranked_view(connection, posted):
    """The chat has to use the write path the CLI does, mirror included."""
    _, keyboard = setting_screen(connection, "DEPAS_PARKING_INCOME", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "+$25.000"))

    mirrored = connection.execute(
        "SELECT value FROM settings WHERE key = 'parking_income'").fetchone()
    assert int(mirrored["value"]) == 25_000


def test_a_trait_offers_only_the_three_things_it_can_mean(connection, posted):
    _, keyboard = setting_screen(connection, "DEPAS_FURNISHED", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "👎 Castigar"))

    assert Preferences.load(connection).value("DEPAS_FURNISHED") == "penalise"


def test_borrar_returns_a_setting_to_its_default(connection, posted):
    set_preference(connection, "DEPAS_AGE_TARGET", "10")
    _, keyboard = setting_screen(connection, "DEPAS_AGE_TARGET", Preferences.load(connection))

    _press(connection, _data_for(keyboard, configure.CLEAR))

    assert Preferences.load(connection).value("DEPAS_AGE_TARGET") == 25


# -- the closed sets, offered rather than typed ----------------------------------


def test_a_commune_is_ticked_and_unticked_from_the_checklist(connection, posted):
    set_preference(connection, "DEPAS_COMMUNES", "providencia")
    _, keyboard = setting_screen(connection, "DEPAS_COMMUNES", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "⬜ Cerrillos"))
    assert "cerrillos" in Preferences.load(connection).communes()

    _, keyboard = setting_screen(connection, "DEPAS_COMMUNES", Preferences.load(connection))
    _press(connection, _data_for(keyboard, "✅ Cerrillos"))
    assert "cerrillos" not in Preferences.load(connection).communes()


def test_the_commune_checklist_pages_rather_than_sending_forty_three_rows(connection):
    _, keyboard = setting_screen(connection, "DEPAS_COMMUNES", Preferences.load(connection))

    assert "1/4" in _labels(keyboard)
    assert "⬜ Vitacura" not in _labels(keyboard)


def test_a_metro_line_moves_tier_without_anybody_writing_the_string(connection, posted):
    set_preference(connection, "DEPAS_METRO_TIERS", "1 > 3,6")
    _, keyboard = setting_screen(connection, "DEPAS_METRO_TIERS", Preferences.load(connection))

    # L2 is in no tier yet, so its first-tier button is the one without a tick.
    _press(connection, _data_for(keyboard, "1"))

    assert Preferences.load(connection).metro_tiers() == [["1", "2"], ["3", "6"]]


def test_a_line_can_be_dropped_out_of_every_tier(connection, posted):
    set_preference(connection, "DEPAS_METRO_TIERS", "1 > 3,6")
    _, keyboard = setting_screen(connection, "DEPAS_METRO_TIERS", Preferences.load(connection))
    out = [row for row in keyboard["inline_keyboard"]
           if row[0]["text"] == "L1"][0][-1]["callback_data"].removeprefix(PREFIX)

    _press(connection, out)

    assert Preferences.load(connection).metro_tiers() == [["3", "6"]]


def test_the_conserjeria_picker_offers_what_a_listing_could_actually_carry(connection):
    connection.execute("UPDATE listings SET security_type = 'diurna'") if connection.execute(
        "SELECT count(*) c FROM listings").fetchone()["c"] else None

    _, keyboard = setting_screen(connection, "DEPAS_SECURITY_WANTED",
                                 Preferences.load(connection))

    assert "24 horas" in _labels(keyboard)


# -- the open sets, which are the only things typed ------------------------------


def test_a_typed_value_finds_its_setting_through_the_prompt_it_answers(connection, posted):
    """No pending-edit table: the prompt names the setting and the reply quotes it back."""
    handled = answer_prompt(connection, None,
                            _message("42", replying="⚙️ DEPAS_AREA_MIN · reemplazar"),
                            Preferences.load(connection))

    assert handled
    assert Preferences.load(connection).value("DEPAS_AREA_MIN") == 42


def test_a_message_that_answers_no_prompt_falls_through(connection, posted):
    """Everything else the bot reads a message for still has to happen."""
    assert not answer_prompt(connection, None, _message("hola"), Preferences.load(connection))
    assert not answer_prompt(connection, None,
                             _message("hola", replying="una respuesta cualquiera"),
                             Preferences.load(connection))


def test_a_typed_value_the_parser_refuses_is_said_where_it_was_typed(connection, posted):
    answer_prompt(connection, None,
                  _message("mucho", replying="⚙️ DEPAS_AREA_MIN · reemplazar"),
                  Preferences.load(connection))

    assert "whole number" in posted["sent"][0][0]
    assert Preferences.load(connection).value("DEPAS_AREA_MIN") is None


def test_an_admin_is_appended_rather_than_replacing_the_list(connection, posted):
    answer_prompt(connection, None, _message("999", replying="⚙️ DEPAS_ADMINS · agregar"),
                  Preferences.load(connection))

    assert Preferences.load(connection).admins() == [ADMIN, 999]


def test_the_last_admin_cannot_be_removed(connection, posted):
    """Emptying it is the one edit the chat could not undo: nobody would be left to."""
    _, keyboard = setting_screen(connection, "DEPAS_ADMINS", Preferences.load(connection))

    _press(connection, _data_for(keyboard, f"❌ {ADMIN}"))

    assert posted["toasts"] == [LAST_ADMIN]
    assert Preferences.load(connection).admins() == [ADMIN]


def test_the_admin_list_offers_no_way_to_clear_it_either(connection):
    _, keyboard = setting_screen(connection, "DEPAS_ADMINS", Preferences.load(connection))

    assert configure.CLEAR not in _labels(keyboard)


def test_a_place_is_typed_as_an_address_and_stored_as_coordinates(connection, posted, monkeypatch):
    monkeypatch.setattr("depas.configure.resolve_locations",
                        lambda fetcher, raw: ("pega,-33.41720,-70.60600",
                                              ["pega → Avenida Providencia 1234"]))

    answer_prompt(connection, None,
                  _message("pega, Avenida Providencia 1234",
                           replying="⚙️ DEPAS_LOCATIONS · agregar"),
                  Preferences.load(connection))

    places = Preferences.load(connection).locations()
    assert [(place.name, place.lat) for place in places] == [("pega", -33.4172)]


def test_a_place_can_be_removed_by_pressing_the_one_you_mean(connection, posted):
    set_preference(connection, "DEPAS_LOCATIONS",
                   "pega,-33.41720,-70.60600; gimnasio,-33.49830,-70.61140")
    _, keyboard = setting_screen(connection, "DEPAS_LOCATIONS", Preferences.load(connection))

    _press(connection, _data_for(keyboard, "❌ pega"))

    assert [place.name for place in Preferences.load(connection).locations()] == ["gimnasio"]


# -- your own flat, field by field rather than as JSON ---------------------------


def test_the_home_is_built_one_press_at_a_time_and_only_saved_once_complete(connection, posted):
    """The setting refuses a half-filled home, so the draft is held apart until it is not."""
    for field, value in (("price_clp", 800_000), ("common_expenses", 130_000), ("area_m2", 62)):
        _press(connection, f"i:{field}:{value}")

    assert Preferences.load(connection).current_home() is None

    _press(connection, "i:commune:nunoa")
    assert Preferences.load(connection).current_home() is None  # lat/lon still missing


def test_typing_an_address_completes_the_home_and_stores_it(connection, posted, monkeypatch):
    monkeypatch.setattr("depas.configure.resolve_locations",
                        lambda fetcher, raw: ("casa,-33.45590,-70.59780",
                                              ["casa → Avenida Los Leones 500"]))
    for field, value in (("price_clp", 800_000), ("common_expenses", 130_000), ("area_m2", 62)):
        _press(connection, f"i:{field}:{value}")

    answer_prompt(connection, None,
                  _message("Los Leones 500", replying="⚙️ DEPAS_CURRENT_HOME · direccion"),
                  Preferences.load(connection))

    home = Preferences.load(connection).current_home()
    assert home == {"price_clp": 800_000, "common_expenses": 130_000, "area_m2": 62,
                    "lat": -33.4559, "lon": -70.5978}


def test_an_incomplete_home_says_what_it_is_still_missing(connection, posted):
    _press(connection, "i:price_clp:800000")

    assert "Falta" in posted["edited"][0][0] or "falta" in posted["toasts"][0]


def test_a_stale_button_says_so_rather_than_raising(connection, posted):
    """A keyboard from before a deploy that renamed something must not cost a traceback."""
    _press(connection, "s:NO_SUCH_SETTING")

    assert posted["toasts"] == [configure.STALE]


# -- the bot routes to it --------------------------------------------------------


def test_the_bot_answers_config_with_the_menu(connection, posted):
    """The command reaches the menu through the same loop that reads every message."""
    from depas.bot import _handle

    _handle(connection, None, _message("/config"), Preferences.load(connection))

    assert "Configuración" in posted["sent"][0][0]


def test_the_bot_addressed_by_name_is_still_the_bot(connection, posted):
    """/config@depas_bot is what Telegram sends where more than one bot is listening."""
    from depas.bot import _handle

    _handle(connection, None, _message("/config@depas_bot"), Preferences.load(connection))

    assert "Configuración" in posted["sent"][0][0]


def test_a_config_press_never_reaches_the_verdict_buttons(connection, posted):
    """Both keyboards ride the same callback queue, so the routing has to tell them apart."""
    from depas.bot import _handle_callback

    _handle_callback(connection, {"id": "1", "data": PREFIX + "g:weights",
                                  "from": {"id": ADMIN},
                                  "message": {"chat": {"id": 5}, "message_id": 9}},
                     Preferences.load(connection))

    assert "Pesos" in posted["edited"][0][0]


def test_an_answered_prompt_is_not_also_scanned_for_listing_links(connection, posted,
                                                                  monkeypatch):
    """A typed setting is not a message posted to be graded, whatever it contains."""
    from depas.bot import _handle

    cards = []
    monkeypatch.setattr("depas.bot.send_listing",
                        lambda *args, **kwargs: cards.append(args) or {})
    monkeypatch.setattr("depas.configure.resolve_locations",
                        lambda fetcher, raw: ("casa,-33.4,-70.6", ["casa → x"]))

    # A name that happens to carry a link still answers the prompt and nothing else.
    _handle(connection, None,
            _message("casa, https://www.portalinmobiliario.com/MLC-999-x-_JM",
                     replying="⚙️ DEPAS_LOCATIONS · agregar"),
            Preferences.load(connection))

    assert cards == []
    assert [place.name for place in Preferences.load(connection).locations()] == ["casa"]


def test_every_screen_survives_being_parsed_as_html(connection, posted):
    """Screens are sent with parse_mode=HTML, and Telegram rejects a stray < or >.

    The metro help and its example are literally `1 > 3,6 > 2,4,4A,5`, so this is not
    hypothetical: unescaped, that one screen fails to send at all.
    """
    from html.parser import HTMLParser

    class Strict(HTMLParser):
        def error(self, message):
            raise AssertionError(message)

    prefs = Preferences.load(connection)
    texts = [main_screen()[0]] + [group_screen(key, prefs)[0] for key in GROUPS]
    texts += [setting_screen(connection, name, prefs)[0] for name in BY_NAME]
    texts += [configure._prompt(name, configure.REPLACE) for name in BY_NAME]
    for text in texts:
        assert "<" not in text.replace("<b>", "").replace("</b>", "")\
            .replace("<i>", "").replace("</i>", "").replace("<code>", "")\
            .replace("</code>", ""), text
        Strict(convert_charrefs=True).feed(text)


def test_a_typed_value_carrying_a_tag_cannot_break_the_answer(connection, posted):
    """The parser's complaint quotes back what was typed, and that answer is HTML."""
    answer_prompt(connection, None,
                  _message("<b>x", replying="⚙️ DEPAS_AREA_MIN · reemplazar"),
                  Preferences.load(connection))

    # The screen's own <b> is still there; what must not survive is the typed one.
    assert "<b>x" not in posted["sent"][0][0]
    assert "&lt;b&gt;x" in posted["sent"][0][0]
