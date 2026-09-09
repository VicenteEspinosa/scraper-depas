import sqlite3
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import HTTPError

from depas.cli import _enrich_one, _infer_stored_descriptions
from depas.detail import INFERRED_VERSION, infer_from_description
from depas.models import Listing
from depas.store import connect, save


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


def _raising(status: int):
    def fetch_detail(fetcher, url):
        raise HTTPError(f"HTTP Error {status}: ", 0, SimpleNamespace(status_code=status))
    return fetch_detail


def test_a_delisted_listing_does_not_stop_the_pass(connection, monkeypatch):
    """A detail page taken down between the search and the fetch 404s; the pass carries on."""
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _raising(404))

    assert _enrich_one(connection, None, _pending(connection)) is False

    assert _pending(connection) is not None  # left unenriched, so out of the alerting pool


def test_a_broken_portal_still_fails_loudly(connection, monkeypatch):
    """Only a 404 means this one listing is gone; any other status is the portal's problem."""
    monkeypatch.setattr("depas.portals.portalinmobiliario.fetch_detail", _raising(403))

    with pytest.raises(HTTPError):
        _enrich_one(connection, None, _pending(connection))


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
