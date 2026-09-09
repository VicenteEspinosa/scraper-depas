"""Cutting a sweep short, and the check that it is safe to."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from depas.communes import Commune
from depas.models import Listing, Query
from depas.portals import chilepropiedades, portalinmobiliario
from depas.store import (
    connect,
    cutoff_safety,
    due_a_deep_sweep,
    known_ids,
    remember_sweep,
    save,
)


@pytest.fixture
def connection(tmp_path):
    return connect(tmp_path / "test.db")


# -- what a page of nothing new means ---------------------------------------------


def _page(*external_ids: str) -> list[Listing]:
    return [Listing(portal="pi", external_id=one, url=f"https://x/{one}",
                    price=500_000, currency="CLP") for one in external_ids]


def test_a_page_we_have_all_of_is_quiet():
    query = Query(known=frozenset({"a", "b"}))

    assert query.nothing_new(_page("a", "b")) is True


def test_one_unknown_listing_makes_a_page_loud():
    query = Query(known=frozenset({"a"}))

    assert query.nothing_new(_page("a", "new")) is False


def test_an_empty_page_is_not_quiet():
    """Nothing to conclude from it; the portal's own "no cards" check ends the sweep."""
    assert Query(known=frozenset({"a"})).nothing_new([]) is False


def test_nothing_is_quiet_when_we_know_nothing():
    assert Query().nothing_new(_page("a")) is False


# -- the cutoff, against a fake portal --------------------------------------------


FIXTURES = Path(__file__).parent / "fixtures"
# The saved card carries one real id; the sweep needs a different one per page, so each
# page is that card with its id rewritten. The markup stays the portal's own.
PI_CARD = (FIXTURES / "pi_unit_card.html").read_text()
PI_REAL_ID = "MLC-4117541988"
CP_CARD = (FIXTURES / "cp_unit_card.html").read_text()
CP_REAL_ID = "124607880"


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _PortalInmobiliario:
    """Serves one real card per page, counting the pages actually asked for."""

    validators: dict = {}

    def __init__(self, ids_per_page: list[list[str]]) -> None:
        self.ids_per_page, self.asked = ids_per_page, 0

    def get(self, url, **kwargs):
        offset = int(url.rsplit("_Desde_", 1)[1].split("_")[0]) if "_Desde_" in url else 1
        index = (offset - 1) // portalinmobiliario.PAGE_SIZE
        self.asked += 1
        ids = self.ids_per_page[index] if index < len(self.ids_per_page) else []
        return _Response("".join(PI_CARD.replace(PI_REAL_ID, one) for one in ids))


def _swept(ids_per_page: list[list[str]], known: set[str],
           quiet_pages: int) -> tuple[list, int]:
    portal = _PortalInmobiliario(ids_per_page)
    query = Query(communes=[Commune("nunoa")], known=frozenset(known),
                  quiet_pages=quiet_pages, max_pages=5)
    found = list(portalinmobiliario.search(portal, query))
    return found, portal.asked


ONE_PER_PAGE = [[f"MLC-{number}"] for number in range(1, 6)]
ALL_FIVE = {f"MLC-{number}" for number in range(1, 6)}


def test_the_sweep_stops_after_enough_quiet_pages():
    found, asked = _swept(ONE_PER_PAGE, ALL_FIVE, quiet_pages=2)

    assert asked == 2  # two quiet pages and it gives up
    assert [one.external_id for one in found] == ["MLC-1", "MLC-2"]


def test_a_find_resets_the_count():
    """One stale page between finds must not end a sweep that is still turning things up."""
    # quiet, find, quiet, quiet -> it takes two *consecutive* quiet ones to stop, so the
    # sweep reaches page four rather than giving up at page one.
    _, asked = _swept(ONE_PER_PAGE, ALL_FIVE - {"MLC-2"}, quiet_pages=2)

    assert asked == 4


def test_zero_reads_every_page():
    """What a deep sweep passes, and what a portal whose ordering we distrust gets."""
    _, asked = _swept(ONE_PER_PAGE, ALL_FIVE, quiet_pages=0)

    assert asked == 5


def test_nothing_is_lost_when_the_cutoff_does_not_fire():
    found, _ = _swept([["MLC-1", "MLC-2"], ["MLC-3"]], set(), quiet_pages=2)

    assert [one.external_id for one in found] == ["MLC-1", "MLC-2", "MLC-3"]


def test_a_listing_behind_known_ones_is_still_found_on_a_deep_sweep():
    """The safety net doing its job: the ordering is wrong here and nothing is lost."""
    out_of_order = [["MLC-1"], ["MLC-2"], ["MLC-999"]]

    shallow, _ = _swept(out_of_order, {"MLC-1", "MLC-2"}, quiet_pages=2)
    deep, _ = _swept(out_of_order, {"MLC-1", "MLC-2"}, quiet_pages=0)

    assert "MLC-999" not in [one.external_id for one in shallow]
    assert "MLC-999" in [one.external_id for one in deep]


def test_each_listing_carries_the_page_it_came_from():
    """Which is the only way to tell later whether the cutoff could have dropped one."""
    found, _ = _swept([["MLC-1"], ["MLC-2"]], set(), quiet_pages=0)

    assert [one.extra["page"] for one in found] == [0, 1]


class _Chilepropiedades:
    validators: dict = {}

    def __init__(self, ids_per_page: list[list[str]]) -> None:
        self.ids_per_page, self.asked = ids_per_page, 0

    def get(self, url, **kwargs):
        page = int(url.rstrip("/").rsplit("/", 1)[1])
        self.asked += 1
        ids = self.ids_per_page[page] if page < len(self.ids_per_page) else []
        return _Response("".join(CP_CARD.replace(CP_REAL_ID, one) for one in ids))


def test_chilepropiedades_cuts_the_same_way():
    """The other portal that paginates; the two share the rule, not the markup."""
    portal = _Chilepropiedades([["111"], ["222"], ["333"]])
    query = Query(communes=[Commune("nunoa")], known=frozenset({"111", "222", "333"}),
                  quiet_pages=1, max_pages=5)

    list(chilepropiedades.search(portal, query))

    assert portal.asked == 1  # one quiet page is enough at quiet_pages=1


# -- the safety net and the measurement -------------------------------------------


def test_a_portal_never_swept_deep_is_due(connection):
    assert due_a_deep_sweep(connection, "pi", hours=24) is True


def test_a_portal_swept_deep_recently_is_not_due(connection):
    remember_sweep(connection, "pi", "nunoa", datetime.now(UTC).isoformat(), 40, None,
                   deep=True, pages_read=5)

    assert due_a_deep_sweep(connection, "pi", hours=24) is False


def test_a_deep_sweep_comes_round_again(connection):
    long_ago = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    remember_sweep(connection, "pi", "nunoa", long_ago, 40, None, deep=True, pages_read=5)

    assert due_a_deep_sweep(connection, "pi", hours=24) is True


def test_a_shallow_sweep_does_not_count_as_deep(connection):
    remember_sweep(connection, "pi", "nunoa", datetime.now(UTC).isoformat(), 40, None,
                   deep=False, pages_read=2)

    assert due_a_deep_sweep(connection, "pi", hours=24) is True


def test_zero_hours_makes_every_sweep_deep(connection):
    """Which switches the cutoff off entirely, the pre-cutoff behaviour."""
    remember_sweep(connection, "pi", "nunoa", datetime.now(UTC).isoformat(), 40, None,
                   deep=True, pages_read=5)

    assert due_a_deep_sweep(connection, "pi", hours=0) is True


def test_the_ordering_holding_reports_nothing(connection):
    """New listings only ever on the first page: the cutoff cannot have dropped one."""
    remember_sweep(connection, "pi", "nunoa", datetime.now(UTC).isoformat(), 40, None,
                   deep=True, pages_read=5, deepest_new_page=0)

    assert cutoff_safety(connection, quiet_pages=2) == []


def test_a_find_past_the_cutoff_is_reported(connection):
    """The assumption failing. Nothing else would have told us."""
    remember_sweep(connection, "pi", "nunoa", datetime.now(UTC).isoformat(), 40, None,
                   deep=True, pages_read=5, deepest_new_page=3)

    risky = cutoff_safety(connection, quiet_pages=2)

    assert [(row["portal"], row["deepest"]) for row in risky] == [("pi", 3)]


def test_only_deep_sweeps_are_evidence(connection):
    """A shallow sweep never looked past the cutoff, so it can prove nothing about it.

    Deliberately deeper than the cutoff, so this would be reported if the query counted
    shallow sweeps: the point is that it does not.
    """
    remember_sweep(connection, "pi", "nunoa", datetime.now(UTC).isoformat(), 40, None,
                   deep=False, pages_read=5, deepest_new_page=4)

    assert cutoff_safety(connection, quiet_pages=2) == []


# -- what the sweep hands the portals ---------------------------------------------


def test_what_a_portal_already_stored_is_handed_to_it(connection):
    save(connection, [Listing(portal="houm", external_id="mine", url="u",
                              price=5e5, currency="CLP", price_clp=5e5),
                      Listing(portal="pi", external_id="theirs", url="u",
                              price=5e5, currency="CLP", price_clp=5e5)])

    assert known_ids(connection, "houm") == frozenset({"mine"})
    assert known_ids(connection, "pi") == frozenset({"theirs"})
