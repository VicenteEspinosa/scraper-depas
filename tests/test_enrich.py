import sqlite3
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import HTTPError

from depas.cli import _enrich_one
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
