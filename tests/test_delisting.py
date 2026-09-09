"""What a sweep saw, and what that lets us conclude about a listing being gone."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from depas.cli import _discover
from depas.communes import Commune
from depas.models import Listing, Query
from depas.shortlist import format_shortlist, starred
from depas.store import (
    KEPT,
    LIKE,
    Subscriber,
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

    rendered = format_shortlist(connection, prefs(), Subscriber("-100123"))

    assert "ya no está" in rendered
    assert len(starred(connection, prefs(), Subscriber("-100123"))) == 1


# -- sweeping every portal at once ------------------------------------------------


def _portal(name: str, listings: list[Listing], error: Exception | None = None):
    """A stand-in portal module: `search` is all `_sweep_portal` ever calls."""
    def search(fetcher, query):
        if error is not None:
            raise error
        yield from listings
    return SimpleNamespace(NAME=name, search=search)


def test_every_portal_is_swept_and_written(connection):
    portals = {"a": _portal("a", [_listing("a1")]), "b": _portal("b", [_listing("b1")])}

    counts = _discover(connection, 39_000.0, Query(communes=[Commune("nunoa")]), portals)

    assert (counts["new"], counts["portals"], counts["failed"]) == (2, 2, 0)
    assert sorted(row["external_id"] for row
                  in connection.execute("SELECT external_id FROM listings")) == ["a1", "b1"]


def test_one_portal_failing_does_not_cost_the_others(connection):
    """Raising here used to abort the pass, and with it every other portal's alerts."""
    portals = {"ok": _portal("ok", [_listing("kept")]),
               "broken": _portal("broken", [], error=RuntimeError("markup moved"))}

    counts = _discover(connection, 39_000.0, Query(communes=[Commune("nunoa")]), portals)

    assert (counts["new"], counts["portals"], counts["failed"]) == (1, 1, 1)
    assert [row["external_id"] for row
            in connection.execute("SELECT external_id FROM listings")] == ["kept"]


def test_a_failed_sweep_is_recorded_as_no_evidence(connection):
    """A comuna that was never looked at must not later read as one that came back empty."""
    portals = {"broken": _portal("broken", [], error=RuntimeError("markup moved")),
               "ok": _portal("ok", [_listing("kept")])}

    _discover(connection, 39_000.0, Query(communes=[Commune("nunoa")]), portals)

    run = connection.execute(
        "SELECT ok, cards_seen, error FROM scrape_runs WHERE portal = 'broken'").fetchone()
    assert (run["ok"], run["cards_seen"]) == (0, 0)
    assert "markup moved" in run["error"]


def test_every_portal_failing_is_still_a_failure(connection):
    """One portal down is noise; none of them answering is the pass being broken."""
    portals = {"a": _portal("a", [], error=RuntimeError("down")),
               "b": _portal("b", [], error=RuntimeError("down"))}

    with pytest.raises(RuntimeError, match="every sweep failed"):
        _discover(connection, 39_000.0, Query(communes=[Commune("nunoa")]), portals)


def test_a_portal_that_does_not_do_this_operation_is_skipped(connection):
    portals = {"sales-only": _portal("sales-only", [],
                                     error=NotImplementedError("rentals only"))}

    counts = _discover(connection, 39_000.0, Query(communes=[Commune("nunoa")]), portals)

    assert (counts["portals"], counts["failed"]) == (0, 0)
    assert connection.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 0


def test_each_comuna_is_swept_and_recorded_on_its_own(connection):
    """One comuna's markup breaking must not make the portal's others look swept."""
    portals = {"a": _portal("a", [_listing("x")])}

    _discover(connection, 39_000.0,
              Query(communes=[Commune("nunoa"), Commune("providencia")]), portals)

    assert sorted(row["commune"] for row
                  in connection.execute("SELECT commune FROM scrape_runs")) == [
        "nunoa", "providencia"]


def test_uf_prices_are_normalised_without_a_request(connection):
    """`_matching` used to normalise through the fetcher, once per portal, over the wire."""
    in_uf = Listing(portal="a", external_id="uf", url="https://x/uf",
                    price=20.0, currency="UF")
    portals = {"a": _portal("a", [in_uf])}

    _discover(connection, 39_000.0, Query(communes=[Commune("nunoa")]), portals)

    assert connection.execute("SELECT price_clp FROM listings").fetchone()[0] == 780_000.0
