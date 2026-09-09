"""Two readers: a verdict belongs to a person, an announcement to a destination."""
import pytest

from depas.cli import _announce
from depas.models import Listing
from depas.shortlist import starred
from depas.store import (
    DISLIKE,
    LIKE,
    Subscriber,
    add_subscriber,
    clear_notified,
    connect,
    mark_notified,
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
    add_subscriber(connection, CHANNEL)
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
    add_subscriber(connection, CHANNEL)
    add_subscriber(connection, "-2002", owner_user_id=ANA)
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
    add_subscriber(connection, CHANNEL)
    add_subscriber(connection, "-2002")
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
