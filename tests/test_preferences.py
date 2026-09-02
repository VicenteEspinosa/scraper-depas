"""The settings live in the database: seeded from .env once, edited from anywhere after."""
import re
from pathlib import Path

import pytest

from depas.cli import _announce, _requirement_clauses
from depas.models import Listing
from depas.config import defaults
from depas.preferences import (DEFAULTED, SET, SETTINGS, UNSET, Preferences,
                               check_environment, clear_preference, described,
                               seed_from_env, set_preference, setting)
from depas.store import connect, forget_preference, save, save_detail, store_preference


def _listing(price: float = 600_000) -> Listing:
    return Listing(portal="houm", external_id="42", url="https://x/42", price=price,
                   currency="CLP", price_clp=price, area_m2=50.0)


@pytest.fixture
def connection(tmp_path):
    return connect(tmp_path / "test.db")


def _stored(connection, name):
    row = connection.execute("SELECT value FROM preferences WHERE name = ?", (name,)).fetchone()
    return row["value"] if row else None


# -- seeding ---------------------------------------------------------------------


def test_the_environment_seeds_the_table_on_the_first_connect(tmp_path, monkeypatch):
    """A box upgrading into this keeps whatever its .env said, without being touched."""
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    monkeypatch.setenv("DEPAS_COMMUNES", "nunoa,santiago")

    connection = connect(tmp_path / "test.db")

    assert _stored(connection, "DEPAS_COST_TARGET") == "850000"
    assert Preferences.load(connection).communes() == ["nunoa", "santiago"]


def test_a_setting_cleared_from_the_chat_stays_cleared_across_a_restart(tmp_path, monkeypatch):
    """Seeding once is the whole point: re-reading .env would undo every edit on reboot."""
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    path = tmp_path / "test.db"
    clear_preference(connect(path), "DEPAS_COST_TARGET")

    reopened = connect(path)

    assert _stored(reopened, "DEPAS_COST_TARGET") is None
    assert Preferences.load(reopened).value("DEPAS_COST_TARGET") is None


def test_the_database_wins_over_the_environment(tmp_path, monkeypatch):
    """Once seeded, .env is history: what is stored is what the bot runs on."""
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    path = tmp_path / "test.db"
    set_preference(connect(path), "DEPAS_COST_TARGET", "700000")

    assert Preferences.load(connect(path)).value("DEPAS_COST_TARGET") == 700_000


def test_importing_the_environment_again_has_to_be_asked_for(tmp_path, monkeypatch):
    """The escape hatch for a box whose .env is the source of truth after all."""
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    connection = connect(tmp_path / "test.db")
    set_preference(connection, "DEPAS_COST_TARGET", "700000")

    assert seed_from_env(connection) == []
    assert "DEPAS_COST_TARGET" in seed_from_env(connection, force=True)
    assert Preferences.load(connection).value("DEPAS_COST_TARGET") == 850_000


# -- writing ---------------------------------------------------------------------


def test_a_value_that_would_not_parse_never_reaches_the_table(connection):
    """Refusing at the write is the point of a registry: a bad value must not wait to bite."""
    set_preference(connection, "DEPAS_COST_TARGET", "850000")

    with pytest.raises(ValueError, match="whole number"):
        set_preference(connection, "DEPAS_COST_TARGET", "ochocientos mil")

    assert _stored(connection, "DEPAS_COST_TARGET") == "850000"


def test_a_commune_that_does_not_exist_is_refused(connection):
    """A typo in a slug would silently narrow the crawl to nothing at all."""
    with pytest.raises(ValueError, match="narnia"):
        set_preference(connection, "DEPAS_COMMUNES", "nunoa,narnia")


def test_a_half_filled_home_is_refused(connection):
    """The same rule /compare relied on, now applied when the value is written."""
    with pytest.raises(ValueError, match="common_expenses"):
        set_preference(connection, "DEPAS_CURRENT_HOME", '{"price_clp": 800000}')


# -- who may configure the bot from a chat ---------------------------------------


def test_nobody_configures_the_bot_from_a_chat_until_somebody_is_named(connection):
    """The default has to be closed: an unset whitelist cannot mean everybody."""
    prefs = Preferences.load(connection)

    assert prefs.admins() == []
    assert not prefs.is_admin(467291452)


def test_an_admin_is_recognised_and_nobody_else_is(connection):
    set_preference(connection, "DEPAS_ADMINS", "467291452, 87654321")
    prefs = Preferences.load(connection)

    assert prefs.is_admin(467291452)
    assert prefs.is_admin(87654321)
    assert not prefs.is_admin(11111111)


def test_a_message_with_no_author_is_never_an_admin(connection):
    """A channel post is signed by the channel, so there is no person to authorise."""
    set_preference(connection, "DEPAS_ADMINS", "467291452")

    assert not Preferences.load(connection).is_admin(None)


def test_a_username_is_refused_where_an_id_is_wanted(connection):
    """A username can be given away and reclaimed; a whitelist keyed on one changes hands."""
    with pytest.raises(ValueError, match="not a username"):
        set_preference(connection, "DEPAS_ADMINS", "@VicenteEspinosa")


def test_an_unknown_setting_is_refused_with_a_suggestion():
    """A misremembered name is the common case, so the error says what was probably meant."""
    with pytest.raises(ValueError, match="DEPAS_COST_TARGET"):
        setting("DEPAS_COST_TARGGET")


def test_a_blank_value_clears_the_setting(connection):
    """Emptying a field is how you turn a preference off, in .env and in the chat alike."""
    set_preference(connection, "DEPAS_SECURITY_WANTED", "24 horas")

    set_preference(connection, "DEPAS_SECURITY_WANTED", "  ")

    assert Preferences.load(connection).value("DEPAS_SECURITY_WANTED") is None


# -- defaults --------------------------------------------------------------------


def test_an_unset_setting_falls_back_to_its_default(connection):
    """Antigüedad is the standing rule, so clearing it moves nothing."""
    prefs = Preferences.load(connection)

    assert prefs.value("DEPAS_AGE_TARGET") == 25
    assert prefs.weights()["cost"] == 1.0
    assert prefs.lease_income("parking") == 0


def test_clearing_a_setting_with_no_default_turns_it_off(connection):
    """Most preferences are opt-in: unset has to mean off, never an invented opinion."""
    set_preference(connection, "DEPAS_WALK_TARGET", "10")
    clear_preference(connection, "DEPAS_WALK_TARGET")

    assert Preferences.load(connection).value("DEPAS_WALK_TARGET") is None


def test_listing_the_settings_says_where_each_value_came_from(connection):
    """`depas config` has to distinguish a deliberate value from a default standing in."""
    set_preference(connection, "DEPAS_COST_TARGET", "850000")
    sources = {declared.name: source for declared, _, source in
               described(Preferences.load(connection))}

    assert sources["DEPAS_COST_TARGET"] == SET
    assert sources["DEPAS_AGE_TARGET"] == DEFAULTED
    assert sources["DEPAS_WALK_TARGET"] == UNSET


# -- what reads them -------------------------------------------------------------


def test_the_alert_filters_are_built_from_the_stored_settings(connection):
    """The watch pass filters on what the database says, not on what the process started with."""
    set_preference(connection, "DEPAS_COST_MAX", "900000")
    set_preference(connection, "DEPAS_COMMUNES", "nunoa")

    conditions, parameters = _requirement_clauses(Preferences.load(connection))

    assert "net_monthly_clp <= ?" in conditions
    assert 900_000 in parameters and "nunoa" in parameters


def test_an_edited_budget_changes_what_is_announced(connection, monkeypatch):
    """End to end: a ceiling written to the table is a ceiling the next pass respects."""
    posted = []
    monkeypatch.setattr("depas.cli.send_listing", lambda chat, text, image=None, thread=None,
                        buttons=None: (posted.append(text),
                                       {"chat": {"id": -100}, "message_id": 1})[1])
    monkeypatch.setattr("depas.cli.chat_type", lambda chat: "channel")
    monkeypatch.setattr("depas.cli.time.sleep", lambda _: None)
    set_preference(connection, "TELEGRAM_CHAT_ID", "-100")
    for index, price in enumerate((500_000, 900_000)):
        save(connection, [Listing(portal="pi", external_id=str(index), url=f"https://x/{index}",
                                  price=price, currency="CLP", price_clp=price, area_m2=50.0)])
        save_detail(connection, "pi", str(index), {"common_expenses": 50_000})
    set_preference(connection, "DEPAS_COST_MAX", "600000")

    assert _announce(connection, Preferences.load(connection), limit=10) == 1


def _net(connection):
    return connection.execute("SELECT net_monthly_clp FROM listings_ranked").fetchone()[0]


def test_editing_the_sublet_income_moves_the_net_cost_the_view_reports(connection):
    """`net_monthly_clp` is a view column, so an edited income has to be pushed into SQL."""
    save(connection, [_listing(600_000)])
    save_detail(connection, "houm", "42", {"common_expenses": 100_000, "parking_spaces": 1})

    store_preference(connection, "DEPAS_PARKING_INCOME", "60000")

    assert _net(connection) == 700_000 - 60_000


def test_clearing_the_sublet_income_moves_it_back(connection):
    """The one write path has to re-mirror on the way out too, not only on the way in."""
    save(connection, [_listing(600_000)])
    save_detail(connection, "houm", "42", {"common_expenses": 100_000, "parking_spaces": 1})
    store_preference(connection, "DEPAS_PARKING_INCOME", "60000")

    forget_preference(connection, "DEPAS_PARKING_INCOME")

    assert _net(connection) == 700_000


# -- the registry itself ---------------------------------------------------------


SEED_FILE = Path(__file__).resolve().parents[1] / "seed.env"


@pytest.fixture
def seeded(monkeypatch):
    """The real seed.env, which the hermetic fixture otherwise hides from every test."""
    monkeypatch.setattr("depas.config.SEED_FILE", SEED_FILE)
    return defaults()


def test_the_seed_file_only_names_settings_that_exist(seeded):
    """seed.env is applied verbatim, so a stale name there is a knob that silently does nothing."""
    assert seeded
    assert set(seeded) <= {declared.name for declared in SETTINGS}


def test_every_seeded_value_survives_its_own_parser(seeded):
    """A checked-in default that does not parse would stop the first connect of a fresh clone."""
    for name, raw in seeded.items():
        setting(name).parse(name, raw)


def test_a_fresh_database_comes_up_configured(tmp_path, monkeypatch):
    """The point of the file: a clone that was never configured still scrapes something."""
    monkeypatch.setattr("depas.config.SEED_FILE", SEED_FILE)

    prefs = Preferences.load(connect(tmp_path / "fresh.db"))

    assert prefs.communes()
    assert prefs.cost.maximum is not None
    # Placeholders, but real ones: without any the commute component never scores and
    # the ceiling filters nothing, so a fresh clone would look like it ignored both.
    assert len(prefs.locations()) == 2


def test_every_declared_setting_parses_its_own_example():
    """An example that does not survive its own parser is documentation that lies."""
    for declared in SETTINGS:
        if declared.example:
            declared.parse(declared.name, declared.example)


def test_every_declared_default_parses():
    """A default is what an unset setting means, so it has to be a value like any other."""
    for declared in SETTINGS:
        if declared.default is not None:
            declared.parse(declared.name, declared.default)


# -- checking the environment before anything is restarted -----------------------


def test_a_valid_environment_reports_no_problems(monkeypatch):
    """The happy path a deploy takes: every value parses, nothing to say."""
    monkeypatch.setenv("DEPAS_COST_TARGET", "850000")
    monkeypatch.setenv("DEPAS_COMMUNES", "nunoa,santiago")

    assert check_environment() == (2, [])


def test_a_value_the_parsers_refuse_is_reported_rather_than_raised(monkeypatch):
    """Every problem at once: a deploy should not be a game of fixing them one per push."""
    monkeypatch.setenv("DEPAS_COMMUNES", "nunoa,las condes")
    monkeypatch.setenv("DEPAS_AVAILABILITY_TARGET", "01/11/2026")

    _, problems = check_environment()

    assert len(problems) == 2
    assert any("las condes" in problem for problem in problems)
    assert any("YYYY-MM-DD" in problem for problem in problems)


def test_a_key_that_is_not_a_setting_is_reported_too(monkeypatch):
    """The quieter failure: a misspelled name is skipped by the seed, never refused."""
    monkeypatch.setenv("DEPAS_ALERT_MAX_PRICE", "900000")

    _, problems = check_environment()

    assert problems == ["DEPAS_ALERT_MAX_PRICE is not a setting, so it would be ignored"]


def test_the_bootstrap_variables_are_not_mistaken_for_typos(monkeypatch):
    """They are read before a database exists, so they are absent from the registry on purpose."""
    monkeypatch.setenv("DEPAS_DB_PATH", "/data/depas.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")

    assert check_environment() == (0, [])


# -- the rename to DEPAS_<PARAMETER>_<SLOT> ---------------------------------------


def test_the_rename_moves_the_value_not_just_the_name(tmp_path):
    """A box upgrading into the new names keeps whatever it had, edits from the chat included."""
    path = tmp_path / "renamed.db"
    old = connect(path)
    old.execute("DELETE FROM preferences")
    old.executemany(
        "INSERT INTO preferences (name, value, updated_at) VALUES (?, ?, '2026-01-01')",
        [("DEPAS_TARGET_COST", "777000"), ("DEPAS_ALERT_MAX_COST", "999000"),
         ("DEPAS_WEIGHT_LOCATION", "3"), ("DEPAS_ALERT_SECURITY", "24 horas")])
    old.execute("DELETE FROM schema_migrations WHERE version = 11")
    old.commit()
    old.close()

    prefs = Preferences.load(connect(path))

    assert (prefs.cost.target, prefs.cost.maximum) == (777000, 999000)
    assert prefs.weights()["walk"] == 3.0
    assert prefs.security_wanted() == "24 horas"


def test_a_parameter_only_declares_the_slots_it_has(connection):
    """Age never blocks an alert and nobody caps area, so those slots stay None."""
    prefs = Preferences.load(connection)

    assert prefs.age.maximum is None
    assert prefs.area.maximum is None
    assert prefs.cost.minimum is None


def test_every_rename_migration_lands_on_names_that_exist():
    """A migration renaming a row to a name nothing declares would silently drop the value."""
    migrations = (Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")
    renamed = re.findall(r"SET name = '([A-Z_]+)'", "".join(
        sql.read_text() for sql in migrations))

    assert renamed
    assert {name for name in renamed} <= {declared.name for declared in SETTINGS}


def test_the_entrega_keeps_its_value_when_it_stops_being_a_bound(tmp_path):
    """The date somebody set as a deadline is the date the new component scores against."""
    path = tmp_path / "entrega.db"
    old = connect(path)
    old.execute("INSERT INTO preferences (name, value, updated_at) "
                "VALUES ('DEPAS_AVAILABLE_BY', '2026-11-15', '2026-01-01')")
    old.execute("DELETE FROM schema_migrations WHERE version = 12")
    old.commit()
    old.close()

    prefs = Preferences.load(connect(path))

    assert prefs.value("DEPAS_AVAILABILITY_TARGET") == "2026-11-15"
