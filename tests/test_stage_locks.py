"""One stage at a time, and a lock that lets go by itself if nobody else does."""
from datetime import UTC, datetime, timedelta

import pytest

from depas.cli import Busy, _stage
from depas.store import (
    connect,
    held_stage_locks,
    release_stage_lock,
    stored_watch,
    take_stage_lock,
)


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPAS_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    return connect(tmp_path / "test.db")


def test_two_processes_cannot_hold_the_same_stage(connection):
    """One statement, so a race cannot let both of them in."""
    assert take_stage_lock(connection, "enrich") is True

    assert take_stage_lock(connection, "enrich") is False


def test_two_different_stages_do_not_wait_for_each_other(connection):
    """They are separate crontab entries on purpose; the lock is per stage."""
    assert take_stage_lock(connection, "enrich") is True

    assert take_stage_lock(connection, "announce") is True


def test_letting_go_lets_the_next_run_start_on_time(connection):
    take_stage_lock(connection, "enrich")

    release_stage_lock(connection, "enrich")

    assert take_stage_lock(connection, "enrich") is True
    assert [row["stage"] for row in held_stage_locks(connection)] == ["enrich"]


def test_a_lock_nobody_released_is_not_a_lock_forever(connection):
    """OOM, SIGKILL, the container restarted mid-run: worse than the overlap it prevents."""
    take_stage_lock(connection, "enrich")
    connection.execute("UPDATE stage_locks SET taken_at = ?",
                       ((datetime.now(UTC) - timedelta(hours=2)).isoformat(),))
    connection.commit()

    assert take_stage_lock(connection, "enrich", timedelta(minutes=30)) is True


# ── what the stage does with it ─────────────────────────────────────────────────


def test_a_stage_already_running_refuses_to_start(connection):
    take_stage_lock(connection, "enrich")

    with pytest.raises(Busy), _stage("enrich"):
        pass


def test_a_stage_that_never_started_says_neither_done_nor_failed(connection):
    """The heartbeat is what `healthcheck` reads: a skipped run must not touch it.

    Stamping success would hide a stage that is wedged, and stamping an error would
    warn the admins about work that is being done perfectly well by somebody else.
    """
    take_stage_lock(connection, "discover")

    with pytest.raises(Busy), _stage("discover"):
        pass

    completed, error = stored_watch(connection, "discover")
    assert (completed, error) == (None, None)


def test_a_stage_that_finished_lets_go(connection):
    with _stage("enrich"):
        assert [row["stage"] for row in held_stage_locks(connection)] == ["enrich"]

    assert held_stage_locks(connection) == []


def test_a_stage_that_raised_lets_go_too(connection):
    """Otherwise one crash would cost every run until the staleness limit expired."""
    with pytest.raises(ValueError), _stage("enrich"):
        raise ValueError("el portal cambió de markup")

    assert held_stage_locks(connection) == []
    _, error = stored_watch(connection, "enrich")
    assert "markup" in error
