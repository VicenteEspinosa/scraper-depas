"""Two readers: a verdict belongs to a person, an announcement to a destination."""
import sqlite3

import pytest

from depas.cli import _announce
from depas.models import Listing
from depas.shortlist import starred
from depas.store import (
    DISLIKE,
    LIKE,
    MIGRATIONS_DIR,
    Subscriber,
    add_subscriber,
    clear_notified,
    connect,
    mark_notified,
    migrate,
    pending_detail,
    pool_query,
    remove_subscriber,
    save,
    save_detail,
    set_interest,
    subscribers,
)
from tests.support import prefs

CHANNEL, ANA, BETO = "-1001", 111, 222


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    connection = connect(tmp_path / "test.db")
    for number in range(3):
        save(connection, [Listing(portal="houm", external_id=str(number),
                                  url=f"https://x/{number}", price=500_000,
                                  currency="CLP", price_clp=500_000.0, area_m2=50.0)])
        save_detail(connection, "houm", str(number), {"common_expenses": 100_000})
    return connection


def _pool(connection, subscriber) -> list[str]:
    return [row["external_id"] for row
            in connection.execute(pool_query(prefs(), subscriber))]


# -- who is subscribed ------------------------------------------------------------


def test_the_configured_chat_stands_in_until_somebody_subscribes(connection, monkeypatch):
    """The box this lands on keeps posting where it posted before, and nowhere else."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHANNEL)

    assert [one.chat_id for one in subscribers(connection, prefs())] == [CHANNEL]


def test_a_real_subscriber_replaces_the_stand_in(connection, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHANNEL)
    add_subscriber(connection, "-2002", owner_user_id=ANA)

    found = subscribers(connection, prefs())

    assert [(one.chat_id, one.owner) for one in found] == [("-2002", ANA)]


def test_nowhere_to_post_is_not_an_error(connection):
    """A fresh install has no destination; whoever needs one says so itself."""
    assert subscribers(connection, prefs()) == []


def test_a_chat_id_has_to_be_one(connection):
    """It reaches SQL as a literal, since a view cannot take a bound parameter."""
    with pytest.raises(ValueError, match="a chat id is a number"):
        add_subscriber(connection, "-100; DROP TABLE listings")


def test_removing_a_subscriber_keeps_what_it_was_told(connection):
    """So re-adding it later is quiet rather than a replay of everything."""
    add_subscriber(connection, CHANNEL, catch_up=True)
    mark_notified(connection, CHANNEL, "houm", "0")

    assert remove_subscriber(connection, CHANNEL) is True
    assert subscribers(connection, prefs()) == []
    assert connection.execute(
        "SELECT COUNT(*) FROM subscriber_notifications").fetchone()[0] == 1


# -- a verdict belongs to a person ------------------------------------------------


def test_one_persons_dislike_no_longer_empties_everybody_elses_pool(connection):
    """The whole point: Ana turning a flat down used to take it out of Beto's pool too."""
    set_interest(connection, "houm", "0", DISLIKE, "ana", ANA)

    assert _pool(connection, Subscriber("-1", ANA)) == ["1", "2"]
    assert _pool(connection, Subscriber("-2", BETO)) == ["0", "1", "2"]


def test_a_shared_chat_counts_anybodys_verdict(connection):
    """Which is what a couple reading one channel together already had."""
    set_interest(connection, "houm", "0", DISLIKE, "ana", ANA)

    assert _pool(connection, Subscriber(CHANNEL)) == ["1", "2"]


def test_two_people_can_disagree(connection):
    set_interest(connection, "houm", "0", LIKE, "ana", ANA)
    set_interest(connection, "houm", "0", DISLIKE, "beto", BETO)

    assert _pool(connection, Subscriber("-1", ANA)) == ["0", "1", "2"]
    assert _pool(connection, Subscriber("-2", BETO)) == ["1", "2"]


def test_each_person_sees_their_own_stars(connection):
    set_interest(connection, "houm", "0", LIKE, "ana", ANA)
    set_interest(connection, "houm", "1", LIKE, "beto", BETO)

    assert [row["external_id"] for row, _
            in starred(connection, prefs(), Subscriber("-1", ANA))] == ["0"]
    assert [row["external_id"] for row, _
            in starred(connection, prefs(), Subscriber("-2", BETO))] == ["1"]


def test_taking_a_verdict_back_removes_it(connection):
    """"I take that back" is the absence of an opinion, not a row that says so."""
    set_interest(connection, "houm", "0", DISLIKE, "ana", ANA)
    set_interest(connection, "houm", "0", None, "ana", ANA)

    assert connection.execute("SELECT COUNT(*) FROM user_interest").fetchone()[0] == 0
    assert _pool(connection, Subscriber("-1", ANA)) == ["0", "1", "2"]


def test_the_detail_queue_only_gives_up_when_nobody_wants_it(connection):
    """One person's dislike must not stop the other from ever seeing the flat."""
    save(connection, [Listing(portal="houm", external_id="new", url="https://x/new",
                              price=500_000, currency="CLP", price_clp=500_000.0)])
    set_interest(connection, "houm", "new", DISLIKE, "ana", ANA)

    assert [row["external_id"] for row in pending_detail(connection, 10)] == []

    set_interest(connection, "houm", "new", LIKE, "beto", BETO)

    assert [row["external_id"] for row in pending_detail(connection, 10)] == ["new"]


# -- an announcement belongs to a destination -------------------------------------


def test_each_destination_is_told_once(connection, monkeypatch):
    """Per subscriber, so a second chat is not a repeat in the first."""
    monkeypatch.setenv("DEPAS_GRADE_MIN", "0")
    add_subscriber(connection, CHANNEL, catch_up=True)
    add_subscriber(connection, "-2002", owner_user_id=ANA, catch_up=True)
    posted = []
    monkeypatch.setattr("depas.cli._post_card",
                        lambda conn, p, dest, row, text: posted.append((dest, row["external_id"])))
    monkeypatch.setattr("depas.cli.time.sleep", lambda seconds: None)

    _announce(connection, prefs(), limit=10)

    assert sorted({chat for chat, _ in posted}) == ["-1001", "-2002"]
    assert len(posted) == 6  # three listings, each to both destinations
    # And a second pass repeats nothing.
    posted.clear()
    _announce(connection, prefs(), limit=10)
    assert posted == []


def test_a_destination_that_refuses_does_not_cost_the_others(connection, monkeypatch):
    """A bot removed from one channel must not silence every other subscriber."""
    monkeypatch.setenv("DEPAS_GRADE_MIN", "0")
    add_subscriber(connection, CHANNEL, catch_up=True)
    add_subscriber(connection, "-2002", catch_up=True)
    monkeypatch.setattr("depas.cli.time.sleep", lambda seconds: None)
    posted = []

    def refuses(conn, p, dest, row, text):
        if dest == CHANNEL:
            raise RuntimeError("bot is not a member of the channel")
        posted.append(dest)

    monkeypatch.setattr("depas.cli._post_card", refuses)

    _announce(connection, prefs(), limit=10)

    assert posted == ["-2002", "-2002", "-2002"]


def test_un_stamping_can_be_aimed_at_one_destination(connection):
    mark_notified(connection, CHANNEL, "houm", "0")
    mark_notified(connection, "-2002", "houm", "0")

    assert clear_notified(connection, hours=6, chat_id=CHANNEL) == 1
    assert [row["chat_id"] for row in connection.execute(
        "SELECT chat_id FROM subscriber_notifications")] == ["-2002"]


# -- a chat starts on what happens next -------------------------------------------


def test_a_new_chat_is_not_told_the_backlog(connection, monkeypatch):
    """The first version drew on years of listings at DEPAS_ALERTS_LIMIT a pass."""
    monkeypatch.setenv("DEPAS_GRADE_MIN", "0")
    posted = []
    monkeypatch.setattr("depas.cli._post_card",
                        lambda conn, p, dest, row, text: posted.append(row["external_id"]))
    monkeypatch.setattr("depas.cli.time.sleep", lambda seconds: None)

    written_off = add_subscriber(connection, CHANNEL)

    assert written_off == 3  # every listing already stored
    assert _announce(connection, prefs(), limit=10) == 0
    assert posted == []


def test_what_turns_up_after_subscribing_is_told(connection, monkeypatch):
    """Written off is not deaf: the point is to start, not to stay quiet."""
    monkeypatch.setenv("DEPAS_GRADE_MIN", "0")
    add_subscriber(connection, CHANNEL)
    posted = []
    monkeypatch.setattr("depas.cli._post_card",
                        lambda conn, p, dest, row, text: posted.append(row["external_id"]))
    monkeypatch.setattr("depas.cli.time.sleep", lambda seconds: None)

    save(connection, [Listing(portal="houm", external_id="fresh", url="https://x/f",
                              price=500_000, currency="CLP", price_clp=500_000.0,
                              area_m2=50.0)])
    save_detail(connection, "houm", "fresh", {"common_expenses": 100_000})

    _announce(connection, prefs(), limit=10)

    assert posted == ["fresh"]


def test_catching_up_asks_for_the_backlog(connection):
    assert add_subscriber(connection, CHANNEL, catch_up=True) == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM subscriber_notifications").fetchone()[0] == 0


def test_re_adding_a_chat_does_not_silence_what_it_was_owed(connection):
    """Re-adding must not write off the listings it was legitimately still waiting for."""
    add_subscriber(connection, CHANNEL, catch_up=True)
    save(connection, [Listing(portal="houm", external_id="owed", url="https://x/o",
                              price=500_000, currency="CLP", price_clp=500_000.0)])

    assert add_subscriber(connection, CHANNEL) == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM subscriber_notifications").fetchone()[0] == 0


# -- carrying a single-reader database across --------------------------------------
# This is the one migration in the set that moves data somebody typed. It is checked
# here rather than trusted, because a wrong backfill silently loses verdicts and makes
# every listing ever stored eligible to be announced again.


def _at_015(tmp_path, chat_id: str | None):
    """A database as it stands before this PR, with verdicts and announcements on it."""
    database = sqlite3.connect(tmp_path / "old.db")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE schema_migrations "
                     "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_")[0])
        if version > 15:
            continue
        database.executescript(path.read_text())
        database.execute("INSERT INTO schema_migrations VALUES (?, datetime('now'))",
                         (version,))
    if chat_id is not None:
        database.execute("INSERT INTO preferences (name, value, updated_at) "
                         "VALUES ('TELEGRAM_CHAT_ID', ?, datetime('now'))", (chat_id,))
    for external_id, interest, by, notified in (
        ("liked", 1, "vicente", "2026-09-01T11:00:00"),
        ("hated", -1, "ana", "2026-09-01T11:00:00"),
        ("quiet", None, None, "2026-09-01T11:00:00"),
        ("waiting", None, None, None),
    ):
        database.execute(
            "INSERT INTO listings (portal, external_id, url, price, currency, first_seen,"
            " last_seen, detail_fetched_at, interest, rated_by, rated_at, notified_at)"
            " VALUES ('houm', ?, 'u', 500000, 'CLP', '2026-09-01', '2026-09-01',"
            " '2026-09-01T10:00:00', ?, ?, '2026-09-02T10:00:00', ?)",
            (external_id, interest, by, notified))
    database.executemany("INSERT INTO settings (key, value) VALUES (?, ?)",
                         (("shortlist_chat_id", -1001), ("shortlist_message_id", 4242)))
    database.commit()
    return database


def test_every_verdict_comes_across(tmp_path):
    database = _at_015(tmp_path, CHANNEL)

    migrate(database)

    assert {row["external_id"]: (row["interest"], row["rated_by"]) for row
            in database.execute("SELECT external_id, interest, rated_by FROM user_interest")
            } == {"liked": (1, "vicente"), "hated": (-1, "ana")}


def test_every_announcement_comes_across_under_its_chat(tmp_path):
    """Getting this wrong re-posts every card the channel has ever been shown."""
    database = _at_015(tmp_path, CHANNEL)

    migrate(database)

    assert {(row["chat_id"], row["external_id"]) for row in database.execute(
        "SELECT chat_id, external_id FROM subscriber_notifications")} == {
        (CHANNEL, "liked"), (CHANNEL, "hated"), (CHANNEL, "quiet")}


def test_a_listing_still_owed_a_card_stays_owed(tmp_path):
    """The backfill must not write off what was legitimately still pending."""
    database = _at_015(tmp_path, CHANNEL)

    migrate(database)

    announced = {row[0] for row in database.execute(
        "SELECT external_id FROM subscriber_notifications")}
    assert "waiting" not in announced


def test_the_configured_chat_becomes_a_shared_subscriber(tmp_path):
    database = _at_015(tmp_path, CHANNEL)

    migrate(database)

    assert [(row["chat_id"], row["owner_user_id"]) for row in database.execute(
        "SELECT chat_id, owner_user_id FROM subscribers")] == [(CHANNEL, None)]


def test_the_pinned_list_keeps_its_message(tmp_path):
    database = _at_015(tmp_path, CHANNEL)

    migrate(database)

    assert [(row["chat_id"], row["message_id"]) for row in database.execute(
        "SELECT chat_id, message_id FROM subscriber_shortlist")] == [("-1001", 4242)]


def test_nothing_is_dropped_off_the_disk(tmp_path):
    """Renamed rather than dropped, so a backfill that went wrong can still be redone."""
    database = _at_015(tmp_path, CHANNEL)

    migrate(database)

    kept = database.execute(
        "SELECT COUNT(*) AS verdicts,"
        "       SUM(legacy_notified_at IS NOT NULL) AS announced FROM listings "
        " WHERE legacy_interest IS NOT NULL OR legacy_notified_at IS NOT NULL").fetchone()
    assert (kept["verdicts"], kept["announced"]) == (3, 3)


def test_a_database_with_no_chat_configured_keeps_its_announcements_on_disk(tmp_path):
    """There is no destination to move them under, so they stay recoverable instead."""
    database = _at_015(tmp_path, None)

    migrate(database)

    assert database.execute(
        "SELECT COUNT(*) FROM subscriber_notifications").fetchone()[0] == 0
    assert database.execute(
        "SELECT COUNT(*) FROM listings WHERE legacy_notified_at IS NOT NULL"
    ).fetchone()[0] == 3
