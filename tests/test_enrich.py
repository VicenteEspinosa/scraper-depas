import sqlite3
import threading
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import HTTPError

from depas.cli import _budget, _enrich, _infer_stored_descriptions, enrich
from depas.detail import INFERRED_VERSION, infer_from_description
from depas.models import Listing
from depas.store import connect, pending_detail, save
from tests.support import prefs


@pytest.fixture
def connection(tmp_path):
    connection = connect(tmp_path / "test.db")
    save(connection, [Listing(portal="portalinmobiliario", external_id="1", url="https://x/1",
                              price=500_000, currency="CLP", price_clp=500_000, area_m2=50.0)])
    return connection


def _pending(connection: sqlite3.Connection) -> sqlite3.Row:
    return connection.execute(
        "SELECT * FROM listings WHERE detail_fetched_at IS NULL"
    ).fetchone()


def _stage_fetcher():
    """What `_stage` hands the enrichment: the workers bring their own."""
    return SimpleNamespace(validators={})


def _enrich_all(connection: sqlite3.Connection, fresh: int = 60) -> int:
    return _enrich(connection, _stage_fetcher(), pending_detail(connection, fresh))


def _raising(status: int):
    def fetch_detail(fetcher, url):
        raise HTTPError(f"HTTP Error {status}: ", 0, SimpleNamespace(status_code=status))
    return fetch_detail


def _reading(**detail):
    def fetch_detail(fetcher, url):
        return dict(detail)
    return fetch_detail


def _also(connection: sqlite3.Connection, portal: str, external_id: str) -> None:
    save(connection, [Listing(portal=portal, external_id=external_id,
                              url=f"https://{portal}/{external_id}", price=500_000,
                              currency="CLP", price_clp=500_000, area_m2=50.0)])


def test_a_delisted_listing_does_not_stop_the_pass(connection, monkeypatch):
    """A detail page taken down between the search and the fetch 404s; the pass carries on."""
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _raising(404))

    assert _enrich_all(connection) == 0

    assert _pending(connection) is not None  # left unenriched, so out of the alerting pool
    assert _pending(connection)["delisted_at"] is not None


def test_one_broken_page_no_longer_takes_the_whole_stage_down(connection, monkeypatch):
    """It used to: any status but 404 was re-raised out of the pass.

    So a page answering 403 for good aborted the stage at the same point every time, and
    everything older than it in the queue was never read at all.
    """
    _also(connection, "houm", "2")
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _raising(403))
    monkeypatch.setattr("depas.portals.houm.fetch_detail", _reading(floor=5))

    assert _enrich_all(connection) == 1

    read = connection.execute(
        "SELECT portal FROM listings WHERE detail_fetched_at IS NOT NULL").fetchall()
    assert [row["portal"] for row in read] == ["houm"]


def test_a_page_that_failed_waits_before_taking_another_slot(connection, monkeypatch):
    """A permanently broken page must not spend a slot every ten minutes forever."""
    _also(connection, "houm", "2")
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _raising(403))
    monkeypatch.setattr("depas.portals.houm.fetch_detail", _reading(floor=5))

    _enrich_all(connection)

    broken = connection.execute(
        "SELECT * FROM listings WHERE portal = 'portalinmobiliario'").fetchone()
    # Not delisted — the page failing is about the page, and it may still be for rent.
    assert broken["delisted_at"] is None
    assert broken["detail_due_at"] > broken["first_seen"]  # held out of the queue
    assert pending_detail(connection, 60) == []


def test_a_portal_failing_on_everything_still_fails_loudly(connection, monkeypatch):
    """One listing failing is that listing's problem; all of them is the portal's.

    A stage that reported success on this would hide a portal that moved its markup.
    """
    _also(connection, "portalinmobiliario", "2")
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _raising(500))

    with pytest.raises(RuntimeError, match="every detail page failed"):
        _enrich_all(connection)


def test_the_portals_are_read_at_the_same_time(connection, monkeypatch):
    """The queue was one file across six different hosts, and the delay is per host.

    A barrier is the proof: three pages that each wait for the other two can only get
    through if they are being read concurrently. Sequentially this times out.
    """
    _also(connection, "houm", "2")
    _also(connection, "toctoc", "3")
    together = threading.Barrier(3, timeout=5)

    def fetch_detail(fetcher, url):
        together.wait()
        return {"floor": 5}

    for portal in ("portalinmobiliario", "houm", "toctoc"):
        monkeypatch.setattr(f"depas.portals.{portal}.fetch_detail", fetch_detail)

    assert _enrich_all(connection) == 3


def test_every_portal_reads_with_its_own_polite_delay(connection, monkeypatch):
    """One `Fetcher` per portal, so the delay is counted per host and not across six."""
    sessions = []
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail",
                        lambda fetcher, url: sessions.append(id(fetcher)) or {"floor": 5})
    _also(connection, "houm", "2")
    monkeypatch.setattr("depas.portals.houm.fetch_detail",
                        lambda fetcher, url: sessions.append(id(fetcher)) or {"floor": 5})

    _enrich_all(connection)

    assert len(set(sessions)) == 2


def test_the_validators_the_workers_saw_still_reach_the_stage(connection, monkeypatch):
    """`enrich` prints the coverage, and detail pages are most of what it counts."""
    fetcher = _stage_fetcher()

    def fetch_detail(worker, url):
        worker.validators[url] = ("etag", None, 200)
        return {"floor": 5}

    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", fetch_detail)

    _enrich(connection, fetcher, pending_detail(connection, 60))

    assert fetcher.validators == {"https://x/1": ("etag", None, 200)}


# -- reading the descriptions already stored --------------------------------------


def _with_description(connection: sqlite3.Connection, text: str) -> None:
    connection.execute("UPDATE listings SET description = ?", (text,))
    connection.commit()


def _counting_reader(monkeypatch) -> list[str]:
    """Every description the pass actually reads, so a re-scan cannot hide in a zero."""
    read: list[str] = []

    def reader(text: str) -> dict[str, object]:
        read.append(text)
        return infer_from_description(text)

    monkeypatch.setattr("depas.cli.infer_from_description", reader)
    return read


def test_a_description_is_read_once_and_then_left_alone(connection, monkeypatch):
    """The hourly pass used to re-read every description it had ever stored."""
    _with_description(connection, "Depto en piso 8 con piscina y gimnasio.")
    read = _counting_reader(monkeypatch)

    assert _infer_stored_descriptions(connection) == 1
    row = connection.execute("SELECT floor, has_pool FROM listings").fetchone()
    assert (row["floor"], row["has_pool"]) == (8, 1)

    # Same reader, same prose: the second pass does not open it at all.
    _infer_stored_descriptions(connection)
    assert len(read) == 1


def test_a_smarter_reader_goes_round_again(connection, monkeypatch):
    """Bumping INFERRED_VERSION is the one thing that makes a re-read worth the scan."""
    _with_description(connection, "Depto en piso 8 con piscina.")
    _infer_stored_descriptions(connection)
    connection.execute("UPDATE listings SET floor = NULL")

    monkeypatch.setattr("depas.cli.INFERRED_VERSION", INFERRED_VERSION + 1)

    assert _infer_stored_descriptions(connection) == 1
    assert connection.execute("SELECT floor FROM listings").fetchone()[0] == 8


def test_a_listing_with_no_description_is_never_scanned(connection):
    assert _infer_stored_descriptions(connection) == 0


# -- how much work one pass may do ------------------------------------------------


def test_a_budget_falls_back_to_its_setting(connection, monkeypatch):
    """The flag overrides one run; the setting is the standing value, editable from the chat."""
    monkeypatch.setenv("DEPAS_ENRICH_LIMIT", "7")
    preferences = prefs()

    assert _budget(None, preferences, "DEPAS_ENRICH_LIMIT") == 7
    assert _budget(3, preferences, "DEPAS_ENRICH_LIMIT") == 3


def test_the_budgets_are_what_a_parallel_read_makes_affordable(connection):
    """Deliberate numbers now, not the ones the command-line flags used to hardcode.

    The detail read is spread across the six portals, so 250 a pass is about 40 per
    portal and a couple of minutes of the ten between runs — the ceiling stopped being
    the clock. Routing stays where it was: it is one third-party host and sequential.
    """
    unset = prefs()

    assert (_budget(None, unset, "DEPAS_ENRICH_LIMIT"),
            _budget(None, unset, "DEPAS_COMMUTE_LIMIT"),
            _budget(None, unset, "DEPAS_ALERTS_LIMIT")) == (250, 40, 25)


def test_a_box_that_set_its_own_budget_keeps_it(connection, monkeypatch):
    """Raising a default must never overrule a number somebody chose on purpose."""
    monkeypatch.setenv("DEPAS_ENRICH_LIMIT", "40")

    assert _budget(None, prefs(), "DEPAS_ENRICH_LIMIT") == 40


# -- the rounds, and who gets the budget ------------------------------------------


@pytest.fixture
def staged(connection, tmp_path, monkeypatch):
    """`enrich` end to end: it opens its own connection, and asks nothing of the network."""
    monkeypatch.setenv("DEPAS_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.cli.stored_uf", lambda connection, fetcher: 39_000.0)
    return connection


def _stage_args(**overrides):
    return SimpleNamespace(**{"limit": None, "refresh_limit": None, "rounds": None,
                             **overrides})


def _queue(connection: sqlite3.Connection, how_many: int, portal: str = "houm") -> None:
    """Listings waiting for a first read, each seen a minute after the one before it.

    Distinct `first_seen` values on purpose: `save` stamps one batch with a single
    timestamp, and "newest first" over a tie is whatever order SQLite feels like.
    """
    save(connection, [Listing(portal=portal, external_id=f"q{n:03}",
                              url=f"https://{portal}/q{n}", price=500_000, currency="CLP",
                              price_clp=500_000.0, area_m2=50.0) for n in range(how_many)])
    connection.executemany(
        "UPDATE listings SET first_seen = ? WHERE portal = ? AND external_id = ?",
        [(f"2026-09-01T{n // 60:02}:{n % 60:02}:00+00:00", portal, f"q{n:03}")
         for n in range(how_many)])
    connection.commit()


def test_a_full_queue_is_read_again_within_the_same_run(staged, monkeypatch):
    """A backlog drains in one slot instead of waiting ten minutes per batch."""
    _queue(staged, 7)
    monkeypatch.setattr("depas.portals.houm.fetch_detail", _reading(floor=5))
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _reading(floor=5))
    monkeypatch.setenv("DEPAS_ENRICH_LIMIT", "3")
    monkeypatch.setenv("DEPAS_ENRICH_ROUNDS", "3")

    enrich(_stage_args())

    read = staged.execute(
        "SELECT COUNT(*) FROM listings WHERE detail_fetched_at IS NOT NULL").fetchone()[0]
    assert read == 8  # three rounds of three, and the queue held eight


def test_a_queue_that_emptied_stops_early(staged, monkeypatch):
    """The rounds are for a backlog: with none, asking the portals again is waste."""
    reads = []
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail",
                        lambda fetcher, url: reads.append(url) or {"floor": 5})
    monkeypatch.setenv("DEPAS_ENRICH_ROUNDS", "5")

    enrich(_stage_args())

    assert reads == ["https://x/1"]  # one page, one round


def test_re_reads_alone_never_trigger_another_round(staged, monkeypatch):
    """Measured on the unread half: there are always re-reads due, and they are not urgent.

    Counting the batch as a whole would read as "behind" on every pass and spend every
    round of every hour on work nobody was waiting for.
    """
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _reading(floor=5))
    monkeypatch.setenv("DEPAS_ENRICH_LIMIT", "1")
    monkeypatch.setenv("DEPAS_REFRESH_LIMIT", "1")
    monkeypatch.setenv("DEPAS_ENRICH_ROUNDS", "4")
    enrich(_stage_args())  # reads it once
    staged.execute("UPDATE listings SET detail_due_at = ''")  # and it is due again
    staged.commit()

    reads = []
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail",
                        lambda fetcher, url: reads.append(url) or {"floor": 5})
    enrich(_stage_args())

    assert len(reads) == 1  # the re-read, and no second round on the strength of it


def test_the_oldest_rows_get_a_share_of_every_batch(connection):
    """Newest first is not a queue: every arrival goes in front of what is waiting.

    Without this slice the oldest rows fall further back with each pass, which is how a
    card for a flat first seen in July turns up in September.
    """
    _queue(connection, 40)

    batch = pending_detail(connection, 10)

    waiting = sorted(row["external_id"] for row in batch)
    assert "q000" in waiting  # the very oldest, which newest-first would never reach
    assert "q039" in waiting  # and the newest, which is still most of the batch
    assert len(batch) == 10


def test_the_share_never_costs_the_batch_a_slot(connection):
    """A small queue where the two ends meet comes back one row each, not twice."""
    _queue(connection, 4)

    batch = pending_detail(connection, 10)

    assert len(batch) == len({row["external_id"] for row in batch}) == 5


def test_the_share_is_a_floor_and_not_a_carve_out(connection):
    """With fewer old rows waiting than the share reserves, the newest fill the rest.

    Getting this wrong spends a full budget on a partial batch — and worse, makes the
    round look like it caught up, so a backlog stops draining while it is still there.
    """
    _queue(connection, 30)
    # Only two rows are genuinely old, where a fifth of ten would reserve two slots.
    connection.execute("UPDATE listings SET first_seen = '2026-01-01T00:00:00+00:00' "
                       "WHERE external_id IN ('q000', 'q001')")
    connection.commit()

    batch = pending_detail(connection, 10)

    assert len(batch) == 10
    waiting = {row["external_id"] for row in batch}
    assert {"q000", "q001"} <= waiting  # the old ones still go in
    assert "q029" in waiting            # and the newest still lead the rest
