"""Reading a detail page again: when it is due, and what changed since last time."""
from datetime import UTC, datetime

import pytest

from depas.models import Listing
from depas.store import (
    REFRESH_DAYS,
    connect,
    detail_digest,
    fill_gaps,
    next_detail_read,
    pending_detail,
    save,
    save_detail,
)


@pytest.fixture
def connection(tmp_path):
    return connect(tmp_path / "test.db")


def _listing(external_id: str, price: int = 500_000) -> Listing:
    return Listing(portal="houm", external_id=external_id, url=f"https://x/{external_id}",
                   price=price, currency="CLP", price_clp=float(price), area_m2=50.0)


def _queued(connection, fresh: int = 10, refresh: int = 10) -> list[str]:
    return [row["external_id"] for row in pending_detail(connection, fresh, refresh)]


def _due_now(connection, external_id: str) -> None:
    connection.execute("UPDATE listings SET detail_due_at = '' WHERE external_id = ?",
                       (external_id,))
    connection.commit()


def _changes(connection, external_id: str) -> dict[str, tuple[str | None, str | None]]:
    return {row["field"]: (row["old_value"], row["new_value"]) for row
            in connection.execute(
                "SELECT field, old_value, new_value FROM detail_changes "
                "WHERE external_id = ?", (external_id,))}


# -- what is due ------------------------------------------------------------------


def test_a_listing_never_read_is_due_at_once(connection):
    save(connection, [_listing("new")])

    assert _queued(connection) == ["new"]


def test_a_listing_just_read_is_not_due_again(connection):
    save(connection, [_listing("read")])
    save_detail(connection, "houm", "read", {"floor": 4})

    assert _queued(connection) == []


def test_a_price_that_moved_brings_the_reading_forward(connection):
    """price is refreshed hourly from the card; price_per_m2_uf came from the detail page."""
    save(connection, [_listing("moved", 500_000)])
    save_detail(connection, "houm", "moved", {"floor": 4})
    assert _queued(connection) == []

    save(connection, [_listing("moved", 430_000)])

    assert _queued(connection) == ["moved"]


def test_a_price_that_moved_before_any_reading_does_not_queue_a_reread(connection):
    """There is nothing to re-read: the row has never been read once."""
    save(connection, [_listing("fresh", 500_000)])
    save(connection, [_listing("fresh", 430_000)])

    assert _queued(connection, fresh=0, refresh=10) == []


def test_an_unread_listing_never_waits_behind_a_reread(connection):
    """A month where a thousand rows come due at once must not starve the finds."""
    save(connection, [_listing("old")])
    save_detail(connection, "houm", "old", {"floor": 4})
    _due_now(connection, "old")
    save(connection, [_listing("brand-new")])

    assert _queued(connection, fresh=1, refresh=1)[0] == "brand-new"


def test_the_two_budgets_are_counted_separately(connection):
    for number in range(3):
        save(connection, [_listing(f"read-{number}")])
        save_detail(connection, "houm", f"read-{number}", {"floor": 4})
        _due_now(connection, f"read-{number}")
    for number in range(3):
        save(connection, [_listing(f"unread-{number}")])

    queued = _queued(connection, fresh=2, refresh=1)

    assert len(queued) == 3
    assert sum(one.startswith("unread") for one in queued) == 2


def test_rereading_can_be_switched_off(connection):
    save(connection, [_listing("read")])
    save_detail(connection, "houm", "read", {"floor": 4})
    _due_now(connection, "read")

    assert _queued(connection, fresh=10, refresh=0) == []


def test_a_delisted_listing_is_never_queued(connection):
    save(connection, [_listing("gone")])
    connection.execute("UPDATE listings SET delisted_at = '2026-01-01'")
    connection.commit()

    assert _queued(connection) == []


# -- what changed -----------------------------------------------------------------


def test_the_first_reading_records_no_changes(connection):
    """Nothing moved: the listing had no detail at all before."""
    save(connection, [_listing("first")])

    assert save_detail(connection, "houm", "first", {"floor": 4}) == 0
    assert _changes(connection, "first") == {}


def test_a_field_that_moved_is_recorded_and_replaced(connection):
    """The current value stays on `listings`; the trail lives in its own table."""
    save(connection, [_listing("gc")])
    save_detail(connection, "houm", "gc", {"common_expenses": 90_000})

    assert save_detail(connection, "houm", "gc", {"common_expenses": 120_000}) == 1

    assert _changes(connection, "gc") == {"common_expenses": ("90000", "120000")}
    assert connection.execute(
        "SELECT common_expenses FROM listings").fetchone()[0] == 120_000


def test_a_field_that_did_not_move_is_not_recorded(connection):
    save(connection, [_listing("same")])
    save_detail(connection, "houm", "same", {"floor": 4, "common_expenses": 90_000})

    assert save_detail(connection, "houm", "same",
                       {"floor": 4, "common_expenses": 90_000}) == 0
    assert _changes(connection, "same") == {}


def test_a_field_the_portal_stopped_publishing_is_recorded(connection):
    """A column that used to be filled and now is not is how a parser breaking looks."""
    save(connection, [_listing("lost")])
    save_detail(connection, "houm", "lost", {"common_expenses": 90_000})

    save_detail(connection, "houm", "lost", {"common_expenses": None})

    assert _changes(connection, "lost") == {"common_expenses": ("90000", None)}


def test_both_trails_read_as_one(connection):
    """price_history predates detail_changes; a reader should not have to know that."""
    save(connection, [_listing("both", 500_000)])
    save_detail(connection, "houm", "both", {"common_expenses": 90_000})
    save(connection, [_listing("both", 430_000)])
    save_detail(connection, "houm", "both", {"common_expenses": 120_000})

    fields = [row["field"] for row in connection.execute(
        "SELECT field FROM listing_changes WHERE external_id = 'both'")]

    assert sorted(fields) == ["common_expenses", "price", "price"]


# -- how long until the next reading ----------------------------------------------


def test_standing_still_earns_a_longer_wait(connection):
    """A flat idle for two months is worth a monthly look, not a three-day one."""
    waits = [next_detail_read(count) for count in range(4)]
    days = [(datetime.fromisoformat(when) - datetime.now(UTC)).days for when in waits]

    assert days == [REFRESH_DAYS - 1, 2 * REFRESH_DAYS - 1,
                    4 * REFRESH_DAYS - 1, 8 * REFRESH_DAYS - 1]


def test_the_wait_stops_growing(connection):
    def in_days(count: int) -> int:
        return (datetime.fromisoformat(next_detail_read(count)) - datetime.now(UTC)).days

    assert in_days(3) == in_days(9)


def test_a_reading_that_found_nothing_new_counts_up(connection):
    save(connection, [_listing("idle")])
    save_detail(connection, "houm", "idle", {"floor": 4})

    save_detail(connection, "houm", "idle", {"floor": 4})
    save_detail(connection, "houm", "idle", {"floor": 4})

    assert connection.execute(
        "SELECT detail_unchanged_count FROM listings").fetchone()[0] == 2


def test_a_reading_that_found_something_starts_over(connection):
    save(connection, [_listing("busy")])
    save_detail(connection, "houm", "busy", {"floor": 4})
    save_detail(connection, "houm", "busy", {"floor": 4})

    save_detail(connection, "houm", "busy", {"floor": 9})

    assert connection.execute(
        "SELECT detail_unchanged_count FROM listings").fetchone()[0] == 0


def test_a_page_the_portal_says_is_unchanged_writes_nothing_but_waits_longer(connection):
    save(connection, [_listing("304")])
    save_detail(connection, "houm", "304", {"floor": 4})

    save_detail(connection, "houm", "304", {"floor": 99}, unchanged=True)

    row = connection.execute("SELECT floor, detail_unchanged_count FROM listings").fetchone()
    assert (row["floor"], row["detail_unchanged_count"]) == (4, 1)


def test_the_digest_ignores_when_it_was_read(connection):
    """Otherwise every reading would look like a change, which is the whole point."""
    first = detail_digest({"floor": 4, "detail_fetched_at": "2026-01-01"})
    later = detail_digest({"floor": 4, "detail_fetched_at": "2026-06-01"})

    assert first == later
    assert first != detail_digest({"floor": 5})


def test_an_inferred_backfill_is_not_a_detail_reading(connection):
    """Nothing was fetched: it must not re-digest the row or push the next reading out."""
    save(connection, [_listing("prose")])
    save_detail(connection, "houm", "prose",
                {"floor": 4, "description": "con piscina", "has_pool": 1})
    before = connection.execute(
        "SELECT detail_hash, detail_due_at FROM listings").fetchone()

    fill_gaps(connection, "houm", "prose", {"has_gym": 1})

    after = connection.execute(
        "SELECT detail_hash, detail_due_at, has_gym FROM listings").fetchone()
    assert after["has_gym"] == 1
    assert (after["detail_hash"], after["detail_due_at"]) == (
        before["detail_hash"], before["detail_due_at"])
    assert _changes(connection, "prose") == {}
