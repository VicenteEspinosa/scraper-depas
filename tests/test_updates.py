"""What a chat is told when a listing it already has a card for changes, and why."""
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from depas import updates
from depas.cli import _announce, _report_updates
from depas.models import Listing
from depas.store import (
    MIGRATIONS_DIR,
    Subscriber,
    add_subscriber,
    connect,
    mark_delisted,
    mark_notified,
    migrate,
    remember_card,
    save,
    save_detail,
)
from depas.updates import Change
from tests.support import prefs

CHAT = "-1001"


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DEPAS_GRADE_MIN", "0")
    return connect(tmp_path / "test.db")


@pytest.fixture
def telegram(monkeypatch):
    """Everything the module would say, without any of it leaving the process."""
    said = {"cards": [], "replies": [], "edits": []}
    monkeypatch.setattr("depas.updates.reply",
                        lambda chat, text, thread_id=None, reply_to=None:
                        said["replies"].append((str(chat), text))
                        or {"chat": {"id": int(chat)}, "message_id": 900})
    monkeypatch.setattr("depas.updates.refresh_card",
                        lambda conn, card, prefs: said["edits"].append(card["message_id"]))
    monkeypatch.setattr("depas.cli.send_listing",
                        lambda chat, text, image=None, thread=None, buttons=None:
                        said["cards"].append(text)
                        or {"chat": {"id": int(chat)}, "message_id": 500 + len(said["cards"])})
    monkeypatch.setattr("depas.cli.chat_type", lambda chat: "channel")
    monkeypatch.setattr("depas.cli.hides_comments", lambda chat: False)
    monkeypatch.setattr("depas.cli.post_breakdown", lambda conn, card, prefs: True)
    monkeypatch.setattr("depas.cli.post_arrival_note",
                        lambda conn, card: said["replies"].append(
                            (str(card["chat_id"]), card["arrival_note"]))
                        if card.get("arrival_note") else False)
    return said


def _listing(price: int = 900_000, **fields) -> Listing:
    return Listing(portal="pi", external_id="7", url="https://x/7", price=price,
                   currency="CLP", price_clp=float(price), area_m2=55.0,
                   commune="nunoa", **fields)


def _announced(connection, price: int = 900_000) -> None:
    """A listing this chat has a card for, as a pass that posted one would leave it."""
    save(connection, [_listing(price)])
    save_detail(connection, "pi", "7", {"walk_minutes": 6, "common_expenses": 80_000})
    remember_card(connection, CHAT, 500, "pi", "7")
    mark_notified(connection, CHAT, "pi", "7")


def _later(connection, table: str, column: str) -> None:
    """Push what is already stored into the past, so what follows counts as after it."""
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    connection.execute(f"UPDATE {table} SET {column} = ?", (an_hour_ago,))
    connection.commit()


def _sync(connection, limit: int = 10) -> int:
    return updates.sync(connection, prefs(), Subscriber(CHAT), limit)


def _one_of_many(external_id: str, price: int, url: str | None = None) -> Listing:
    return Listing(portal="pi", external_id=external_id,
                   url=url or f"https://x/{external_id}", price=price, currency="CLP",
                   price_clp=float(price), area_m2=55.0, commune="nunoa")


def _three_announced(connection, price: int = 1_000_000, how_many: int = 3,
                     url: str | None = None) -> None:
    """Listings this chat holds cards for, ready to be moved by `_rebaja_all`."""
    for number in range(how_many):
        save(connection, [_one_of_many(str(number), price, url)])
        save_detail(connection, "pi", str(number), {"walk_minutes": 6})
        remember_card(connection, CHAT, 500 + number, "pi", str(number))
        mark_notified(connection, CHAT, "pi", str(number))


def _rebaja_all(connection, price: int = 900_000, how_many: int = 3,
                url: str | None = None) -> None:
    """Every one of them cut, after everything already stored: one change each to report."""
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    for number in range(how_many):
        save(connection, [_one_of_many(str(number), price, url)])


# ── what counts as a change ─────────────────────────────────────────────────────


def test_a_rebaja_is_read_off_the_price_trail_as_one_move(connection):
    """`price_history` is a trail with no old column; a rebaja is two rows of it."""
    _announced(connection, 1_000_000)
    _later(connection, "price_history", "seen_at")
    save(connection, [_listing(920_000)])

    changed = updates.changes_for(connection, "pi", "7", None)

    assert [str(one) for one in changed] == ["El arriendo bajó de $1.000.000 a $920.000"]


def test_a_price_re_recorded_at_the_same_figure_is_not_a_move(connection):
    """Every pass saves every listing; only a figure that actually moved is news."""
    _announced(connection)
    save(connection, [_listing()])

    assert updates.changes_for(connection, "pi", "7", None) == []


def test_the_clock_moving_is_not_the_flat_changing(connection):
    """`published_days_ago` moves by thirty in a month with nothing about the flat moving."""
    _announced(connection)
    save_detail(connection, "pi", "7", {"published_days_ago": 12, "walk_minutes": 6})
    save_detail(connection, "pi", "7", {"published_days_ago": 19, "walk_minutes": 6})

    assert updates.changes_for(connection, "pi", "7", None) == []


def test_a_gasto_comun_that_moved_is_told_as_money(connection):
    _announced(connection)
    save_detail(connection, "pi", "7", {"common_expenses": 95_000})

    changed = updates.changes_for(connection, "pi", "7", None)

    assert [str(one) for one in changed] == ["El gasto común subió de $80.000 a $95.000"]


def test_a_flag_has_no_direction(connection):
    """An apartment that turned out to be amoblado did not have its amoblado subir."""
    assert str(Change("furnished", "0", "1", "x")) == "El amoblado cambió de no a sí"


def test_a_label_that_names_several_things_conjugates_with_them(connection):
    """«Los estacionamientos subió» is not Spanish, and the article is what says so."""
    assert str(Change("parking_spaces", "0", "1", "x")).startswith(
        "Los estacionamientos subieron")


# ── case one: a card the chat already has ───────────────────────────────────────


def test_a_card_already_posted_is_edited_its_thread_told_and_the_digest_sent(
        connection, telegram):
    """The three things the reader asked for, and in that order."""
    _announced(connection, 1_000_000)
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    assert _sync(connection) == 1

    assert telegram["edits"] == [500]  # the original card, re-rendered in place
    thread, digest = telegram["replies"]
    assert "Cambió desde que te mandé esta tarjeta" in thread[1]
    assert "El arriendo bajó de $1.000.000 a $920.000" in thread[1]
    assert "Cambió lo que ya te mandé" in digest[1]
    assert "El arriendo bajó de $1.000.000 a $920.000" in digest[1]


def test_the_digest_is_one_message_for_every_listing_that_moved(connection, telegram):
    """Ten rebajas are one message: ten notifications is how a chat gets muted."""
    _three_announced(connection)
    _rebaja_all(connection)

    assert _sync(connection) == 3

    digests = [text for chat, text in telegram["replies"]
               if "Cambió lo que ya te mandé" in text]
    assert len(digests) == 1
    assert "3 avisos" in digests[0]


def test_a_change_is_news_exactly_once(connection, telegram):
    """The watermark is what stops the same rebaja arriving every hour after it."""
    _announced(connection, 1_000_000)
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    assert _sync(connection) == 1
    assert _sync(connection) == 0


def test_a_digest_that_could_not_be_posted_is_said_again_next_pass(connection, telegram,
                                                                   monkeypatch):
    """Nothing is stamped until the message is out: a swallowed notice never comes back."""
    _announced(connection, 1_000_000)
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    def refuses(chat, text, thread_id=None, reply_to=None):
        if "Cambió lo que ya te mandé" in text:
            raise RuntimeError("bot is not a member of the channel")
        return {"chat": {"id": int(chat)}, "message_id": 900}

    monkeypatch.setattr("depas.updates.reply", refuses)
    assert _sync(connection) == 0

    monkeypatch.setattr("depas.updates.reply",
                        lambda chat, text, thread_id=None, reply_to=None:
                        telegram["replies"].append((str(chat), text))
                        or {"chat": {"id": int(chat)}, "message_id": 900})
    assert _sync(connection) == 1


def test_the_budget_caps_a_pass_and_says_what_is_left(connection, telegram):
    """Two hundred moved prices is ten minutes of edits and a digest nobody reads."""
    _three_announced(connection)
    _rebaja_all(connection)

    assert _sync(connection, limit=2) == 2

    digest = [text for chat, text in telegram["replies"] if "ya te mandé" in text][0]
    assert "3 avisos" in digest and "…y 1 más" in digest
    # And the one left out is not lost: the next pass says it.
    assert _sync(connection, limit=2) == 1


def test_a_listing_stamped_without_ever_being_posted_is_not_corrected(connection, telegram):
    """Below the bar gets stamped and never posted: there is no card and no reader."""
    save(connection, [_listing(1_000_000)])
    save_detail(connection, "pi", "7", {"walk_minutes": 6})
    mark_notified(connection, CHAT, "pi", "7")  # stamped, but no card
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    assert _sync(connection) == 0
    assert telegram["replies"] == []


def test_a_baja_is_the_change_worth_telling(connection, telegram):
    """«El aviso que te mandé ya no está» is the most useful notice of the set."""
    _announced(connection)
    _later(connection, "subscriber_notifications", "notified_at")
    mark_delisted(connection, "pi", "7")

    assert _sync(connection) == 1

    digest = [text for chat, text in telegram["replies"] if "ya te mandé" in text][0]
    assert "ya no está" in digest
    assert "el portal ya no publica su ficha" in digest


def test_a_vuelta_is_told_too(connection, telegram):
    """What a false positive looks like from the outside, rather than a silent return."""
    _announced(connection)
    _later(connection, "subscriber_notifications", "notified_at")
    mark_delisted(connection, "pi", "7")
    _sync(connection)
    save(connection, [_listing()])

    assert _sync(connection) == 1
    assert "Volvió a estar publicado" in telegram["replies"][-1][1]


def test_the_notice_says_how_the_nota_moved(connection, telegram):
    """The grade a card went out with is recorded, so a notice can compare against it."""
    _announced(connection, 1_000_000)
    connection.execute("UPDATE subscriber_notifications SET grade_letter = 'D', "
                       "grade_score = 40")
    connection.commit()
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    _sync(connection)

    assert "La nota pasó de D 40" in telegram["replies"][0][1]


# ── case two: a card arriving late ──────────────────────────────────────────────


def _stored_weeks_ago(connection, days: int = 21) -> None:
    when = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    connection.execute("UPDATE listings SET first_seen = ?", (when,))
    connection.execute("UPDATE price_history SET seen_at = ?", (when,))
    connection.commit()


def test_a_card_for_a_flat_stored_weeks_ago_says_why_it_is_arriving_now(connection,
                                                                        telegram):
    """The question the reader would otherwise ask, answered under the card."""
    save(connection, [_listing(1_000_000)])
    _stored_weeks_ago(connection)
    save(connection, [_listing(920_000)])  # a rebaja is what got it past the ceiling
    save_detail(connection, "pi", "7", {"walk_minutes": 6})

    _announce(connection, prefs(), limit=10)

    notes = [text for chat, text in telegram["replies"] if "aparece recién ahora" in text]
    assert len(notes) == 1
    assert "cambió el costo" in notes[0]
    assert "no porque hayas tocado tus criterios" in notes[0]
    assert "El arriendo bajó de $1.000.000 a $920.000" in notes[0]


def test_a_flat_announced_the_hour_it_turned_up_explains_itself(connection, telegram):
    """A note under every card is a note nobody reads."""
    save(connection, [_listing()])
    save_detail(connection, "pi", "7", {"walk_minutes": 6})

    _announce(connection, prefs(), limit=10)

    assert telegram["cards"]  # the card went out
    assert [text for chat, text in telegram["replies"] if "aparece recién" in text] == []


def test_a_late_card_with_nothing_moved_blames_the_queue_it_waited_in(connection,
                                                                       telegram):
    """The honest answer when the listing did not change: the wait was ours.

    The detail queue is newest-first, so a flat can sit unenriched for weeks behind the
    ones that turned up after it — which is the likeliest reason of the set.
    """
    save(connection, [_listing()])
    _stored_weeks_ago(connection)
    save_detail(connection, "pi", "7", {"walk_minutes": 6})  # read only today

    _announce(connection, prefs(), limit=10)

    note = [text for chat, text in telegram["replies"] if "aparece recién ahora" in text][0]
    assert "su ficha se leyó hoy" in note
    assert "21 días" in note


def test_a_late_card_that_was_waiting_on_its_commute_says_that(connection, telegram):
    """Routing is its own stage with its own budget, so it is its own kind of wait.

    A listing can be enriched for weeks and still out of the pool because nothing has
    computed its travel time yet, which `json_extract(commute, ...) <= ?` reads as false.
    """
    save(connection, [_listing()])
    save_detail(connection, "pi", "7", {"walk_minutes": 6})
    _stored_weeks_ago(connection)
    connection.execute("UPDATE listings SET detail_fetched_at = first_seen")
    connection.execute(
        "INSERT INTO detail_changes (portal, external_id, field, old_value, new_value,"
        " changed_at) VALUES ('pi', '7', 'commute', NULL, ?, ?)",
        ('{"oficina": 32}', datetime.now(UTC).isoformat()))
    connection.commit()

    _announce(connection, prefs(), limit=10)

    note = [text for chat, text in telegram["replies"] if "aparece recién ahora" in text][0]
    assert "cambió el viaje" in note
    assert "El viaje ahora dice oficina 32 min" in note


def test_the_budget_at_zero_switches_the_whole_thing_off(connection, telegram):
    """Every other budget in the table means "none of this" at zero, and so does this one."""
    _announced(connection, 1_000_000)
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    assert _sync(connection, limit=0) == 0

    assert telegram["replies"] == []
    assert telegram["edits"] == []


def test_a_late_card_for_a_listing_that_came_back_says_so(connection, telegram):
    """Out of the pool entirely is a reason no change to its fields can explain."""
    save(connection, [_listing()])
    save_detail(connection, "pi", "7", {"walk_minutes": 6})
    _stored_weeks_ago(connection)
    mark_delisted(connection, "pi", "7")
    save(connection, [_listing()])

    _announce(connection, prefs(), limit=10)

    note = [text for chat, text in telegram["replies"] if "Volvió a estar" in text][0]
    assert "un barrido lo encontró de nuevo" in note


def test_a_late_card_is_never_also_reported_as_a_correction(connection, telegram):
    """It carries its own explanation; the digest would be the same news twice."""
    save(connection, [_listing(1_000_000)])
    _stored_weeks_ago(connection)
    save(connection, [_listing(920_000)])
    save_detail(connection, "pi", "7", {"walk_minutes": 6})

    _announce(connection, prefs(), limit=10)
    reported = _report_updates(connection, prefs(),
                               type("Args", (), {"updates_limit": 10})())

    assert reported == 0
    assert [text for chat, text in telegram["replies"] if "ya te mandé" in text] == []


def test_a_second_subscriber_is_told_on_its_own_account(connection, telegram):
    """A change is news per chat: what one has been told is nothing to do with the other."""
    _announced(connection, 1_000_000)
    # Both spelled out: the chat standing in for TELEGRAM_CHAT_ID disappears the moment
    # a real subscriber is written, which is `subscribers` doing what it says.
    add_subscriber(connection, CHAT, catch_up=True)
    add_subscriber(connection, "-2002", catch_up=True)
    remember_card(connection, "-2002", 700, "pi", "7")
    mark_notified(connection, "-2002", "pi", "7")
    _later(connection, "price_history", "seen_at")
    _later(connection, "subscriber_notifications", "notified_at")
    save(connection, [_listing(920_000)])

    reported = _report_updates(connection, prefs(),
                              type("Args", (), {"updates_limit": 10})())

    assert reported == 2
    assert {chat for chat, text in telegram["replies"] if "ya te mandé" in text} == {
        CHAT, "-2002"}


# ── the migration, from a database at the version before it ─────────────────────


def _at_017(tmp_path):
    """A database as it stands before this change: cards announced, nothing about changes."""
    database = sqlite3.connect(tmp_path / "old.db")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE schema_migrations "
                     "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_")[0])
        if version > 17:
            continue
        database.executescript(path.read_text())
        database.execute("INSERT INTO schema_migrations VALUES (?, datetime('now'))",
                         (version,))
    database.execute(
        "INSERT INTO listings (portal, external_id, url, price, currency, first_seen,"
        " last_seen, detail_fetched_at) VALUES ('houm', 'told', 'u', 500000, 'CLP',"
        " '2026-09-01', '2026-09-01', '2026-09-01')")
    database.execute(
        "INSERT INTO subscriber_notifications (chat_id, portal, external_id, notified_at)"
        " VALUES (?, 'houm', 'told', '2026-09-01T11:00:00')", (CHAT,))
    database.execute("INSERT INTO card_messages (chat_id, message_id, portal, external_id,"
                     " posted_at) VALUES (?, 42, 'houm', 'told', '2026-09-01T11:00:00')",
                     (CHAT,))
    database.commit()
    return database


def test_a_card_already_announced_keeps_its_stamp_and_gains_no_grade(tmp_path):
    """The grade is only ever what this version wrote: an old card honestly has none.

    Inventing one would make the first notice about it say the nota moved when all that
    moved is that we started recording it.
    """
    database = _at_017(tmp_path)

    migrate(database)

    stamped = database.execute(
        "SELECT notified_at, grade_letter, grade_score FROM subscriber_notifications"
    ).fetchone()
    assert stamped["notified_at"] == "2026-09-01T11:00:00"
    assert (stamped["grade_letter"], stamped["grade_score"]) == (None, None)


def test_a_card_already_posted_starts_on_what_happens_next(tmp_path):
    """Otherwise the first pass after the deploy reports months of accumulated history.

    True, all of it, and none of it news: price movements drip-fed ten an hour and a
    «ya no está» for every flat that came off the market back in July.
    """
    database = _at_017(tmp_path)

    migrate(database)

    through = database.execute("SELECT through FROM update_notifications").fetchone()
    assert through["through"] > "2026-09-01T11:00:00"  # after the card, so nothing is owed
    # And it is comparable with what the code writes: an ISO timestamp carrying its zone.
    assert datetime.fromisoformat(through["through"]).tzinfo is not None


def test_a_listing_stamped_without_a_card_is_not_given_a_watermark(tmp_path):
    """There is no message to correct and nobody who ever saw it: it cannot be reported."""
    database = _at_017(tmp_path)
    database.execute(
        "INSERT INTO subscriber_notifications (chat_id, portal, external_id, notified_at)"
        " VALUES (?, 'houm', 'never-posted', '2026-09-01T11:00:00')", (CHAT,))

    migrate(database)

    assert [row["external_id"] for row
            in database.execute("SELECT external_id FROM update_notifications")] == ["told"]


def test_a_digest_too_long_for_telegram_is_cut_rather_than_lost(connection, telegram):
    """Telegram rejects a message past 4096 characters instead of trimming it.

    So the budget is the message's, not just the pass's: what does not fit stays
    unstamped and is told next time, exactly like what the pass budget pushed out.
    """
    # Portals publish long urls, and every entry carries one twice — as the link and
    # as its text. Six of these is a message Telegram would refuse whole.
    long_url = "https://portal/" + "arriendo-departamento-nunoa-" * 25
    _three_announced(connection, how_many=6, url=long_url)
    _rebaja_all(connection, how_many=6, url=long_url)

    told = _sync(connection, limit=10)

    digest = [text for chat, text in telegram["replies"] if "ya te mandé" in text][0]
    assert len(digest) <= updates.LIMIT
    assert told < 6
    assert "6 avisos" in digest and f"…y {6 - told} más" in digest
    # And what did not fit is still owed rather than quietly written off.
    assert _sync(connection, limit=10) == 6 - told
