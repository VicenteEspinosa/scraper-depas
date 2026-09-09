"""The UF: fetched once a day, and not worth the sweep when the indicator is down."""
import sqlite3
from datetime import date, timedelta

import pytest
from curl_cffi.requests.exceptions import RequestException

from depas.fetch import Fetcher
from depas.store import connect
from depas.uf import stored_uf


class Down(Fetcher):
    """A fetcher whose every request fails, without opening a session."""

    def __init__(self) -> None:  # noqa: D107 -- no session, so nothing to close
        self.validators = {}

    def get(self, url, **kwargs):
        raise RequestException("mindicador.cl: connection refused")


@pytest.fixture
def connection(tmp_path):
    return connect(tmp_path / "test.db")


def test_yesterdays_uf_stands_in_when_the_indicator_is_down(connection, capsys):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    connection.execute("INSERT INTO uf_daily (day, value) VALUES (?, ?)", (yesterday, 39_500.5))
    connection.commit()

    assert stored_uf(connection, Down()) == 39_500.5
    assert "using the UF of" in capsys.readouterr().out


def test_a_stale_stand_in_is_not_written_down_as_todays(connection):
    """Tomorrow's pass must ask the indicator again rather than inherit the fallback."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    connection.execute("INSERT INTO uf_daily (day, value) VALUES (?, ?)", (yesterday, 39_500.5))
    connection.commit()

    stored_uf(connection, Down())

    assert connection.execute(
        "SELECT day FROM uf_daily WHERE day = ?", (date.today().isoformat(),)).fetchone() is None


def test_with_nothing_cached_the_outage_is_still_an_error(connection):
    """A wrong UF would misprice every listing quoted in UF; with no UF there is no sweep."""
    with pytest.raises(RequestException):
        stored_uf(connection, Down())


def test_the_fallback_reads_a_plain_connection_too(tmp_path):
    """Not every caller sets a row factory, so the lookup indexes by position."""
    connect(tmp_path / "test.db").close()
    plain = sqlite3.connect(tmp_path / "test.db")
    plain.execute("INSERT INTO uf_daily (day, value) VALUES ('2020-01-01', 28000)")
    plain.commit()

    assert stored_uf(plain, Down()) == 28000
