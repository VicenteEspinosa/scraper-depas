import argparse
from datetime import UTC, datetime, timedelta

import pytest

from depas.cli import _announce
from depas.models import Listing
from depas.store import DISLIKE, clear_notified, connect, save, save_detail, set_interest
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

    def send(chat, text, image=None, thread=None):
        posted.append((text, image))
        return {"chat": {"id": int(chat)}, "message_id": 500 + len(posted)}

    monkeypatch.setattr("depas.cli.send_listing", send)
    monkeypatch.setattr("depas.cli.chat_type", lambda chat: "channel")
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
    # No amount published: the assumed default is shown, labelled, and never a dash.
    assert "arriendo + $120.000 gastos comunes (estimado por defecto, no publicado)" in card
    assert "—" not in card


def test_the_card_labels_a_published_gasto_comun_as_published():
    """A real figure is shown on its own — the default disclaimer belongs only to guesses."""
    from depas.grade import Scale

    row = {"commune": "nunoa", "area": 50.0, "net_monthly_clp": 600_000,
           "price_clp": 500_000, "common_expenses": 80_000, "url": "https://x/1"}

    card = format_listing(row, Scale([row]).grade(row))

    assert "arriendo + $80.000 gastos comunes" in card
    assert "por defecto" not in card


def test_a_listing_with_a_photo_is_sent_as_one(monkeypatch):
    """sendPhoto carries the card as a caption; without an image it falls back to text."""
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.call",
                        lambda method, **params: calls.append((method, params)) or {})

    from depas.telegram import send_listing

    send_listing("-100", "card", "https://img/1.webp")
    send_listing("-100", "card", None)

    assert [method for method, _ in calls] == ["sendPhoto", "sendMessage"]


def test_a_reply_inside_a_comment_thread_stays_in_it(monkeypatch):
    """A thread id reaches Telegram; without one the parameter is left out entirely."""
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.call",
                        lambda method, **params: calls.append(params) or {})

    from depas.telegram import send_listing

    send_listing("-100", "card", None, 77)
    send_listing("-100", "card", None)

    assert calls[0]["message_thread_id"] == 77
    assert "message_thread_id" not in calls[1]


def test_requirements_gate_which_listings_are_announced(connection, sent, monkeypatch):
    """A listing that misses a configured requirement is never posted."""
    monkeypatch.setenv("DEPAS_ALERT_MAX_WALK", "2")

    posted = _announce(connection, limit=10)

    assert posted == 2  # walk_minutes 1 and 2 qualify; 3 and 4 do not
    assert len(sent) == 2


def test_a_listing_that_misses_a_requirement_stays_eligible(connection, sent, monkeypatch):
    """Unqualified listings are left unstamped, so a later price drop can still alert."""
    monkeypatch.setenv("DEPAS_ALERT_MAX_WALK", "2")
    _announce(connection, limit=10)

    monkeypatch.delenv("DEPAS_ALERT_MAX_WALK")

    assert _announce(connection, limit=10) == 2


def test_unset_requirements_impose_no_filter(connection, sent):
    """With nothing configured, every enriched listing is a candidate."""
    assert _announce(connection, limit=10) == 4


def test_a_rejected_listing_is_never_announced(connection, sent):
    """A /dislike in the chat is final: the card stays away, and `resend` cannot bring it back."""
    set_interest(connection, "pi", "0", DISLIKE, "vicente")

    assert _announce(connection, limit=10) == 3

    clear_notified(connection, hours=6)  # what `depas resend` un-stamps
    _announce(connection, limit=10)

    assert [text for text, _ in sent if "https://x/0" in text] == []


def test_announced_cards_are_recorded_so_a_command_can_find_them(connection, sent):
    """Without the message ids, a /like commented under a card has nothing to match on."""
    _announce(connection, limit=1)

    card = connection.execute("SELECT * FROM card_messages").fetchone()
    assert (card["chat_id"], card["message_id"]) == ("-100", 501)
    assert card["portal"] == "pi"


def test_enrichment_downgrading_bedrooms_blocks_the_alert(connection, sent, monkeypatch):
    """The card said 2D, the detail page says 1D — the alert must respect the corrected value."""
    monkeypatch.setenv("DEPAS_ALERT_MIN_BEDROOMS", "2")
    save(connection, [Listing(portal="pi", external_id="drift", url="https://x/drift",
                              price=600_000, currency="CLP", price_clp=600_000,
                              bedrooms=2, area_m2=50.0)])
    save_detail(connection, "pi", "drift", {"bedrooms": 1, "walk_minutes": 1})

    _announce(connection, limit=10)

    posted_urls = [text for text, _ in sent if "drift" in text]
    assert posted_urls == []


def test_alerts_are_confined_to_the_configured_communes(connection, sent, monkeypatch):
    """A listing from outside DEPAS_ALERT_COMMUNES is never announced."""
    monkeypatch.setenv("DEPAS_ALERT_COMMUNES", "nunoa")
    save(connection, [Listing(portal="pi", external_id="far", url="https://x/far",
                              price=600_000, currency="CLP", price_clp=600_000,
                              commune="las-condes", area_m2=50.0)])
    save_detail(connection, "pi", "far", {"walk_minutes": 1})

    _announce(connection, limit=10)

    assert [text for text, _ in sent if "far" in text] == []


def test_the_card_marks_how_complete_the_grade_is(monkeypatch):
    """A grade missing a component reads ❓; one scored on every axis reads ✔️."""
    from depas.grade import Scale
    from depas.telegram import COMPLETE_MARK, PARTIAL_MARK

    monkeypatch.setenv("DEPAS_TARGET_FLOOR", "5")
    complete = {"commune": "nunoa", "area": 50.0, "net_monthly_clp": 600_000,
                "price_clp": 600_000, "url": "https://x/1", "walk_minutes": 5, "floor": 6}
    thin = complete | {"floor": None, "url": "https://x/2"}
    scale = Scale([complete, thin])

    full_card = format_listing(complete, scale.grade(complete))
    thin_card = format_listing(thin, scale.grade(thin))

    assert COMPLETE_MARK in full_card and PARTIAL_MARK not in full_card
    assert PARTIAL_MARK in thin_card and COMPLETE_MARK not in thin_card


def test_a_test_card_is_marked_as_one():
    """A test send leads with 🧪 so it is never mistaken for a real find."""
    from depas.grade import Scale
    from depas.telegram import TEST_MARK

    row = {"commune": "nunoa", "area": 50.0, "net_monthly_clp": 600_000,
           "price_clp": 600_000, "url": "https://x/1"}
    scale = Scale([row])

    assert format_listing(row, scale.grade(row), is_test=True).startswith(TEST_MARK)
    assert TEST_MARK not in format_listing(row, scale.grade(row))


def test_prose_fills_only_what_the_spec_table_left_empty():
    """A portal's own value always wins; the description is read for the rest."""
    from depas.detail import infer_from_description

    inferred = infer_from_description(
        "Departamento en piso 8, con ascensor y conserjería 24 horas, "
        "sin piscina y gimnasio equipado. Piso flotante en todo el depto.")

    assert inferred["floor"] == 8  # "piso flotante" needs a digit, so it never matches
    assert inferred["has_elevator"] == 1
    assert inferred["has_pool"] == 0
    assert inferred["has_gym"] == 1  # the denial belongs to the pool, not the gym
    assert inferred["security_type"] == "24 horas"


def test_prose_denials_are_read_as_absence():
    """'sin ascensor' must record the absence, not the mention."""
    from depas.detail import infer_from_description

    inferred = infer_from_description("Edificio sin ascensor, no tiene conserjería.")

    assert inferred["has_elevator"] == 0
    assert inferred["has_concierge"] == 0


def test_the_card_carries_the_listing_number(connection):
    """Every card shows the row's id so a listing can be looked up from the group."""
    from depas.grade import Scale

    save(connection, [Listing(portal="p", external_id="1", url="https://x/1", title="t",
                              price=600_000, currency="CLP", commune="providencia",
                              bedrooms=2, area_m2=50.0, price_clp=600_000)])
    row = dict(connection.execute("SELECT * FROM listings_ranked").fetchone())

    card = format_listing(row, Scale([row]).grade(row))

    assert f"[{row['id']}]" in card
    assert row["id"] == 1


def test_a_listing_without_a_commune_leaves_no_dangling_separator():
    """The header joins only the parts that exist, so a missing commune is not an empty slot."""
    from depas.grade import Scale

    row = {"id": 7, "commune": None, "area": 50.0, "net_monthly_clp": 600_000,
           "price_clp": 600_000, "url": "https://x/1"}

    header = format_listing(row, Scale([row]).grade(row)).splitlines()[0]

    assert "·  ·" not in header
    assert header.endswith("<code>[7]</code>")


def test_a_telegram_failure_reports_the_parameters_it_came_with(monkeypatch):
    """A migrated chat carries its new id in `parameters`; the error must not drop it."""
    from depas import telegram

    monkeypatch.setattr(telegram, "bot_token", lambda: "t")
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: type("R", (), {
        "json": staticmethod(lambda: {
            "ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
            "parameters": {"migrate_to_chat_id": -1009999999999}})})())

    with pytest.raises(RuntimeError, match="-1009999999999"):
        telegram.call("sendMessage", chat_id="-123", text="x")


def test_un_stamping_announces_the_listings_again(connection, sent):
    """Moving already-posted cards to another chat is exactly this: clear, then re-announce."""
    _announce(connection, limit=10)

    clear_notified(connection, hours=6)

    assert _announce(connection, limit=10) == 4


def test_un_stamping_leaves_older_alerts_where_they_are(connection, sent):
    """The window is the whole point: a re-point moves the last batch, not the archive."""
    _announce(connection, limit=10)
    connection.execute("UPDATE listings SET notified_at = ? WHERE external_id = '0'",
                       ((datetime.now(UTC) - timedelta(days=2)).isoformat(),))

    cleared = clear_notified(connection, hours=6)

    assert cleared == 3


def test_a_pass_with_nothing_to_post_asks_telegram_nothing(connection, sent, monkeypatch):
    """The destination lookup sits behind the candidate check, so a quiet hour stays quiet."""
    _announce(connection, limit=10)
    monkeypatch.setattr("depas.cli.chat_type",
                        lambda chat: pytest.fail("nothing to post, so nothing to look up"))

    assert _announce(connection, limit=10) == 0


def test_a_furnished_apartment_is_never_announced(connection, sent):
    """Amoblado is a hard no: no grade, no price and no configuration makes it a candidate."""
    save(connection, [Listing(portal="pi", external_id="amoblado", url="https://x/amoblado",
                              price=400_000, currency="CLP", price_clp=400_000, area_m2=60.0)])
    save_detail(connection, "pi", "amoblado", {"walk_minutes": 1, "furnished": 1})

    _announce(connection, limit=10)

    assert [text for text, _ in sent if "amoblado" in text] == []


def test_a_title_that_says_amoblado_is_enough_to_drop_it(connection, sent):
    """The portals that publish no Amoblado spec row still say it in the title."""
    save(connection, [Listing(portal="pi", external_id="titled", url="https://x/titled",
                              title="Departamento amoblado 2D2B", price=400_000,
                              currency="CLP", price_clp=400_000, area_m2=60.0)])
    save_detail(connection, "pi", "titled", {"walk_minutes": 1})

    _announce(connection, limit=10)

    assert [text for text, _ in sent if "titled" in text] == []


def test_a_listing_that_states_it_is_unfurnished_still_alerts(connection, sent):
    """Only a furnished flat is excluded; declaring the field must not cost the alert."""
    save(connection, [Listing(portal="pi", external_id="vacio", url="https://x/vacio",
                              price=400_000, currency="CLP", price_clp=400_000, area_m2=60.0)])
    save_detail(connection, "pi", "vacio", {"walk_minutes": 1, "furnished": 0})

    _announce(connection, limit=10)

    assert [text for text, _ in sent if "vacio" in text] != []


def test_prose_reads_a_furnished_apartment_off_the_description():
    """A portal without an Amoblado spec row still says it in words."""
    from depas.detail import infer_from_description

    assert infer_from_description("Depto amoblado, listo para llegar.")["furnished"] == 1
    assert infer_from_description("Se entrega sin amoblar.")["furnished"] == 0


def test_a_fitted_kitchen_is_not_a_furnished_apartment():
    """"Cocina amoblada" is cabinets — reading it as furniture would drop good listings."""
    from depas.detail import infer_from_description

    assert "furnished" not in infer_from_description("Cocina amoblada y logia independiente.")
    # The kitchen disowns only its own clause, never the sentence that follows it.
    assert infer_from_description(
        "Cocina totalmente amoblada. El departamento se arrienda amoblado.")["furnished"] == 1


def test_the_card_shows_the_age_and_flags_it_when_over_target(monkeypatch):
    """The antigüedad reads in the spec line, and an old building says so in the cons."""
    from depas.grade import Scale

    monkeypatch.setenv("DEPAS_TARGET_AGE", "25")
    young = {"commune": "nunoa", "area": 50.0, "net_monthly_clp": 600_000, "age": 8,
             "price_clp": 600_000, "url": "https://x/1"}
    old = young | {"age": 44, "url": "https://x/2"}
    scale = Scale([young, old])

    assert "8 años" in format_listing(young, scale.grade(young))
    assert "44 años, sobre los 25" in format_listing(old, scale.grade(old))
