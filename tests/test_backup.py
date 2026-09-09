"""`depas backup`: a copy of the database as it stands, taken without migrating it."""
import sqlite3
from argparse import Namespace

import pytest

from depas.cli import backup
from depas.store import connect


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "depas.db"
    path.parent.mkdir()
    monkeypatch.setenv("DEPAS_DB_PATH", str(path))
    connection = connect(path)
    connection.execute("INSERT INTO uf_daily (day, value) VALUES ('2026-09-01', 39000)")
    connection.commit()
    return path


def _copies(folder):
    return sorted(folder.glob("depas-*.db"))


def test_the_copy_lands_beside_the_database_and_holds_its_data(database):
    backup(Namespace(dir=None, keep=5))

    copies = _copies(database.parent / "backups")
    assert len(copies) == 1
    assert sqlite3.connect(copies[0]).execute(
        "SELECT value FROM uf_daily").fetchone()[0] == 39000


def test_the_copy_is_not_migrated_on_the_way_out(tmp_path, monkeypatch):
    """The whole point: the copy predates whatever the code about to run would change."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE listings (portal TEXT)")
    old.commit()
    old.close()
    monkeypatch.setenv("DEPAS_DB_PATH", str(path))

    backup(Namespace(dir=str(tmp_path / "copies"), keep=5))

    copy = sqlite3.connect(next((tmp_path / "copies").glob("old-*.db")))
    assert copy.execute("SELECT name FROM sqlite_master WHERE name = 'schema_migrations'"
                        ).fetchone() is None
    # And the original was left alone too.
    assert sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'").fetchone() is None


def test_older_copies_are_rotated_out_once_the_new_one_is_safe(database, monkeypatch):
    stamps = iter(f"2026090{n}T000000Z" for n in range(1, 5))
    monkeypatch.setattr("depas.cli._backup_stamp", lambda: next(stamps))

    for _ in range(4):
        backup(Namespace(dir=None, keep=2))

    kept = [copy.name for copy in _copies(database.parent / "backups")]
    assert kept == ["depas-20260903T000000Z.db", "depas-20260904T000000Z.db"]


def test_keeping_zero_means_keeping_every_one(database, monkeypatch):
    stamps = iter(f"2026090{n}T000000Z" for n in range(1, 4))
    monkeypatch.setattr("depas.cli._backup_stamp", lambda: next(stamps))

    for _ in range(3):
        backup(Namespace(dir=None, keep=0))

    assert len(_copies(database.parent / "backups")) == 3


def test_a_box_with_no_database_yet_has_nothing_to_copy(tmp_path, monkeypatch, capsys):
    """A first deploy must not fail on the backup step: there is nothing to protect yet."""
    monkeypatch.setenv("DEPAS_DB_PATH", str(tmp_path / "missing.db"))

    backup(Namespace(dir=None, keep=5))

    assert "nothing to copy" in capsys.readouterr().out
    assert not (tmp_path / "backups").exists()
