"""What a sweep saw, and what that lets us conclude about a listing being gone."""
from datetime import UTC, datetime, timedelta

import pytest

from depas.models import Listing
from depas.shortlist import format_shortlist, starred
from depas.store import (
    KEPT,
    LIKE,
    connect,
    mark_delisted,
    quiet_portals,
    remember_sweep,
    save,
    save_detail,
    set_interest,
    sweep_delisted,
)
from tests.support import prefs


@pytest.fixture
def connection(tmp_path):
    return connect(tmp_path / "test.db")


def _listing(external_id: str, price: int = 500_000) -> Listing:
    return Listing(portal="houm", external_id=external_id, url=f"https://x/{external_id}",
                   price=price, currency="CLP", price_clp=float(price), area_m2=50.0)


def _in_pool(connection) -> list[str]:
    return [row["external_id"] for row
            in connection.execute(f"SELECT external_id FROM listings WHERE {KEPT}")]


def _stored(connection, external_id: str, column: str):
    return connection.execute(
        f"SELECT {column} FROM listings WHERE external_id = ?", (external_id,)
    ).fetchone()[0]


def _seen_at(connection, external_id: str, when: datetime) -> None:
    """Pin when a sweep last turned this listing up, which is what the count is against."""
    connection.execute("UPDATE listings SET last_seen = ? WHERE external_id = ?",
                       (when.isoformat(), external_id))
    connection.commit()


def _sweeps(connection, how_many: int, *, since: datetime, cards: int = 12) -> None:
    """Believable sweeps of the portal, each starting after `since`."""
    for number in range(1, how_many + 1):
        remember_sweep(connection, "houm", "nunoa",
                       (since + timedelta(minutes=number)).isoformat(), cards, None)


def _enrol(connection, *external_ids: str) -> datetime:
    """Two enriched listings in the pool, last seen an hour ago."""
    save(connection, [_listing(one) for one in external_ids])
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    for one in external_ids:
        save_detail(connection, "houm", one, {"floor": 5})
        _seen_at(connection, one, an_hour_ago)
    return an_hour_ago


# -- the count of believable sweeps -----------------------------------------------


def test_a_listing_no_sweep_turns_up_leaves_the_pool(connection):
    """An apartment rented three weeks ago used to stay in the pool for good."""
    an_hour_ago = _enrol(connection, "gone")
    _sweeps(connection, 3, since=an_hour_ago)

    assert sweep_delisted(connection, 3) == 1
    assert _in_pool(connection) == []


def test_one_sweep_short_is_not_enough(connection):
    an_hour_ago = _enrol(connection, "maybe")
    _sweeps(connection, 2, since=an_hour_ago)

    assert sweep_delisted(connection, 3) == 0
    assert _in_pool(connection) == ["maybe"]


def test_the_sweep_that_saw_it_does_not_count_against_it(connection):
    """`started_at` is taken before the scrape and `last_seen` written during it."""
    save(connection, [_listing("here")])
    save_detail(connection, "here" and "houm", "here", {"floor": 5})
    started_at = datetime.now(UTC) - timedelta(minutes=5)
    for _ in range(5):
        remember_sweep(connection, "houm", "nunoa", started_at.isoformat(), 12, None)

    assert sweep_delisted(connection, 3) == 0
    assert _in_pool(connection) == ["here"]


def test_a_portal_that_raised_delists_nobody(connection):
    """A comuna that never got looked at must not read later as one that came back empty."""
    an_hour_ago = _enrol(connection, "unknown")
    for number in range(1, 6):
        remember_sweep(connection, "houm", "nunoa",
                       (an_hour_ago + timedelta(minutes=number)).isoformat(), 0,
                       "HTTPError: 503")

    assert sweep_delisted(connection, 3) == 0


def test_a_sweep_that_saw_nothing_delists_nobody(connection):
    """A portal whose markup moved returns zero cards and raises nothing at all."""
    an_hour_ago = _enrol(connection, "unknown")
    _sweeps(connection, 5, since=an_hour_ago, cards=0)

    assert sweep_delisted(connection, 3) == 0


def test_delisting_can_be_switched_off(connection):
    an_hour_ago = _enrol(connection, "gone")
    _sweeps(connection, 9, since=an_hour_ago)

    assert sweep_delisted(connection, 0) == 0


# -- coming back ------------------------------------------------------------------


def test_a_listing_seen_again_comes_back(connection):
    """A portal outage, or a comuna dropped from the config and restored, heals itself."""
    an_hour_ago = _enrol(connection, "back")
    _sweeps(connection, 3, since=an_hour_ago)
    sweep_delisted(connection, 3)
    assert _in_pool(connection) == []

    save(connection, [_listing("back")])

    assert _stored(connection, "back", "delisted_at") is None
    assert _in_pool(connection) == ["back"]


def test_a_404_delists_on_its_own(connection):
    """The portal saying the page is gone beats any number of sweeps not finding it."""
    _enrol(connection, "removed")

    mark_delisted(connection, "houm", "removed")

    assert _in_pool(connection) == []


# -- telling a broken parser from an empty comuna ---------------------------------


def test_a_portal_that_went_quiet_is_reported(connection):
    """Zero cards where there used to be plenty is a parser or a portal, not a market."""
    started = datetime.now(UTC)
    remember_sweep(connection, "houm", "nunoa", started.isoformat(), 40, None)
    remember_sweep(connection, "houm", "nunoa",
                   (started + timedelta(hours=1)).isoformat(), 0, None)

    quiet = quiet_portals(connection)

    assert [(row["portal"], row["cards_at_best"]) for row in quiet] == [("houm", 40)]


def test_a_portal_still_finding_listings_is_not_reported(connection):
    started = datetime.now(UTC)
    remember_sweep(connection, "houm", "nunoa", started.isoformat(), 40, None)
    remember_sweep(connection, "houm", "nunoa",
                   (started + timedelta(hours=1)).isoformat(), 38, None)

    assert quiet_portals(connection) == []


def test_a_portal_that_never_found_anything_is_not_reported(connection):
    """A comuna the portal simply does not index is not news every hour, forever."""
    remember_sweep(connection, "houm", "nunoa", datetime.now(UTC).isoformat(), 0, None)

    assert quiet_portals(connection) == []


# -- the pinned list keeps what it loses ------------------------------------------


def test_a_starred_listing_that_went_away_stays_on_the_list_marked(connection):
    """Dropping it silently answers "what happened to that one?" by losing the question."""
    _enrol(connection, "starred")
    set_interest(connection, "houm", "starred", LIKE)
    mark_delisted(connection, "houm", "starred")

    rendered = format_shortlist(connection, prefs(), "-100123")

    assert "ya no está" in rendered
    assert len(starred(connection, prefs())) == 1
