from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

from depas.cli import healthcheck, watch
from depas.store import connect, remember_watch, stored_watch


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DEPAS_ADMINS", "11,22")
    database = tmp_path / "test.db"
    # A fresh connection per call, as in production: `watch` closes the one it opened.
    monkeypatch.setattr("depas.cli.connect", lambda: connect(database))
    return connect(database)


@pytest.fixture
def warned(monkeypatch):
    sent = []
    monkeypatch.setattr("depas.cli.reply", lambda chat, text: sent.append((chat, text)))
    return sent


def _completed_hours_ago(connection, hours: float) -> None:
    connection.execute("INSERT INTO settings (key, value) VALUES ('watch_completed_at', ?)",
                       ((datetime.now(UTC) - timedelta(hours=hours)).isoformat(),))
    connection.commit()


def test_a_recent_pass_warns_nobody(connection, warned):
    """The hourly pass completing is the whole signal: no message while it keeps completing."""
    remember_watch(connection, None)

    healthcheck(Namespace(stale_hours=4))

    assert warned == []


def test_a_stale_pass_warns_every_admin(connection, warned):
    """Four hours of no completed pass is a crash loop or a dead container, not a quiet market."""
    _completed_hours_ago(connection, 5)

    healthcheck(Namespace(stale_hours=4))

    assert [chat for chat, _ in warned] == ["11", "22"]


def test_the_warning_carries_the_error_that_stopped_the_pass(connection, warned):
    """The admin reads this on a phone, so it says why rather than sending them to the logs."""
    _completed_hours_ago(connection, 5)
    remember_watch(connection, "HTTPError: HTTP Error 404: ")

    healthcheck(Namespace(stale_hours=4))

    assert "HTTPError: HTTP Error 404" in warned[0][1]


def test_a_watch_that_never_completed_warns_too(connection, warned):
    """A deploy whose pass has never finished is exactly what this is meant to catch."""
    healthcheck(Namespace(stale_hours=4))

    assert len(warned) == 2


def test_a_failed_pass_does_not_count_as_a_completed_one(connection):
    """The bug this watchdog was built for scraped fine and died later; only the end counts."""
    remember_watch(connection, "HTTPError: HTTP Error 404: ")

    completed, error = stored_watch(connection)

    assert completed is None and "HTTP Error 404" in error


def test_a_pass_that_dies_records_what_killed_it(connection):
    """The watchdog is only as good as the stamp, so a failing pass has to write one."""
    with pytest.raises(ValueError):  # no communes configured, so the pass dies early
        watch(Namespace(limit=1, refresh_limit=1))

    assert "ValueError: set DEPAS_COMMUNES" in stored_watch(connection)[1]
