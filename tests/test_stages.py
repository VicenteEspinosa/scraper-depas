"""The pass as four stages: each stamps its own heartbeat, and the watchdog reads all."""
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

from depas.cli import PASS_STAGES, healthcheck
from depas.store import STAGES, connect, remember_watch, stale_stages, stored_watch


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DEPAS_ADMINS", "42")
    database = tmp_path / "test.db"
    # A fresh connection per call, as in production: each stage opens its own.
    monkeypatch.setattr("depas.cli.connect", lambda: connect(database))
    return connect(database)


@pytest.fixture
def warned(monkeypatch):
    sent = []
    monkeypatch.setattr("depas.cli.reply", lambda chat, text: sent.append((chat, text)))
    return sent


def _completed(connection, stage: str, when: datetime) -> None:
    remember_watch(connection, None, stage)
    key = "watch_completed_at" if stage == "watch" else f"watch_completed_at:{stage}"
    connection.execute("UPDATE settings SET value = ? WHERE key = ?",
                       (when.isoformat(), key))
    connection.commit()


def _all_completed(connection, when: datetime) -> None:
    for stage in STAGES:
        _completed(connection, stage, when)


# -- one heartbeat per stage ------------------------------------------------------


def test_each_stage_stamps_its_own(connection):
    remember_watch(connection, None, "discover")

    assert stored_watch(connection, "discover")[0] is not None
    assert stored_watch(connection, "enrich")[0] is None
    # And the whole-pass stamp is untouched by any one stage.
    assert stored_watch(connection)[0] is None


def test_the_whole_pass_keeps_the_original_keys(connection):
    """A box upgrading into this must not read as having never completed a pass."""
    remember_watch(connection, None)

    stored = dict(connection.execute("SELECT key, value FROM settings"))
    assert "watch_completed_at" in stored


def test_a_stage_that_dies_records_what_killed_it(connection):
    remember_watch(connection, "HTTPError: 503", "enrich")

    completed, error = stored_watch(connection, "enrich")
    assert completed is None and error == "HTTPError: 503"


# -- what the watchdog considers stale --------------------------------------------


def test_the_whole_pass_stamp_is_not_something_to_alert_on(connection):
    """A box whose crontab drives the stages runs no `watch`, so that stamp never moves."""
    now = datetime.now(UTC)
    _all_completed(connection, now)
    _completed(connection, "watch", now - timedelta(days=30))

    assert stale_stages(connection) == []


def test_a_stalled_stage_is_reported_even_while_the_others_are_fine(connection):
    """The failure this could never see: a scrape that keeps succeeding hides the rest."""
    now = datetime.now(UTC)
    _all_completed(connection, now)
    _completed(connection, "enrich", now - timedelta(hours=9))

    assert [stage for stage, _, _ in stale_stages(connection)] == ["enrich"]


def test_a_stage_that_never_completed_is_still_reported(connection):
    """Skipping an unstamped stage left the four unwatched on a box that had never run."""
    assert [stage for stage, _, _ in stale_stages(connection)] == list(STAGES)


def test_each_stage_has_its_own_patience(connection):
    """Routing is somebody else's server; discovery feeds everything downstream."""
    now = datetime.now(UTC)
    _all_completed(connection, now)
    _completed(connection, "discover", now - timedelta(hours=5))
    _completed(connection, "route", now - timedelta(hours=5))

    assert [stage for stage, _, _ in stale_stages(connection)] == ["discover"]


def test_the_warning_names_every_stalled_stage(connection, warned):
    now = datetime.now(UTC)
    _all_completed(connection, now)
    _completed(connection, "enrich", now - timedelta(hours=9))
    _completed(connection, "route", now - timedelta(hours=25))
    remember_watch(connection, "HTTPError: 503", "route")

    healthcheck(Namespace(stale_hours=None))

    assert len(warned) == 1
    assert "la lectura de fichas" in warned[0][1]
    assert "HTTPError: 503" in warned[0][1]


def test_a_healthy_box_warns_nobody(connection, warned):
    _all_completed(connection, datetime.now(UTC))

    healthcheck(Namespace(stale_hours=None))

    assert warned == []


# -- the stages and the pass agree ------------------------------------------------


def test_the_pass_runs_every_stage_the_watchdog_knows(connection):
    """A stage added to one and not the other is a stage nobody notices stalling."""
    named = {stage.__name__ for stage in PASS_STAGES}

    assert named == set(STAGES)
