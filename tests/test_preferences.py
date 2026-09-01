"""The settings live in the database: seeded from .env once, edited from anywhere after."""
from pathlib import Path

import pytest

from depas.cli import _announce, _requirement_clauses
from depas.models import Listing
from depas.preferences import (BOOTSTRAP, DEFAULTED, SET, SETTINGS, UNSET, Preferences,
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
    monkeypatch.setenv("DEPAS_TARGET_COST", "850000")
    monkeypatch.setenv("DEPAS_ALERT_COMMUNES", "nunoa,santiago")

    connection = connect(tmp_path / "test.db")

    assert _stored(connection, "DEPAS_TARGET_COST") == "850000"
    assert Preferences.load(connection).alert_communes() == ["nunoa", "santiago"]


def test_a_setting_cleared_from_the_chat_stays_cleared_across_a_restart(tmp_path, monkeypatch):
    """Seeding once is the whole point: re-reading .env would undo every edit on reboot."""
    monkeypatch.setenv("DEPAS_TARGET_COST", "850000")
    path = tmp_path / "test.db"
    clear_preference(connect(path), "DEPAS_TARGET_COST")

    reopened = connect(path)

    assert _stored(reopened, "DEPAS_TARGET_COST") is None
    assert Preferences.load(reopened).value("DEPAS_TARGET_COST") is None


def test_the_database_wins_over_the_environment(tmp_path, monkeypatch):
    """Once seeded, .env is history: what is stored is what the bot runs on."""
    monkeypatch.setenv("DEPAS_TARGET_COST", "850000")
    path = tmp_path / "test.db"
    set_preference(connect(path), "DEPAS_TARGET_COST", "700000")

    assert Preferences.load(connect(path)).value("DEPAS_TARGET_COST") == 700_000


def test_importing_the_environment_again_has_to_be_asked_for(tmp_path, monkeypatch):
    """The escape hatch for a box whose .env is the source of truth after all."""
    monkeypatch.setenv("DEPAS_TARGET_COST", "850000")
    connection = connect(tmp_path / "test.db")
    set_preference(connection, "DEPAS_TARGET_COST", "700000")

    assert seed_from_env(connection) == []
    assert "DEPAS_TARGET_COST" in seed_from_env(connection, force=True)
    assert Preferences.load(connection).value("DEPAS_TARGET_COST") == 850_000


# -- writing ---------------------------------------------------------------------


def test_a_value_that_would_not_parse_never_reaches_the_table(connection):
    """Refusing at the write is the point of a registry: a bad value must not wait to bite."""
    set_preference(connection, "DEPAS_TARGET_COST", "850000")

    with pytest.raises(ValueError, match="whole number"):
        set_preference(connection, "DEPAS_TARGET_COST", "ochocientos mil")

    assert _stored(connection, "DEPAS_TARGET_COST") == "850000"


def test_a_commune_that_does_not_exist_is_refused(connection):
    """A typo in a slug would silently narrow the crawl to nothing at all."""
    with pytest.raises(ValueError, match="narnia"):
        set_preference(connection, "DEPAS_ALERT_COMMUNES", "nunoa,narnia")


def test_a_half_filled_home_is_refused(connection):
    """The same rule /compare relied on, now applied when the value is written."""
    with pytest.raises(ValueError, match="common_expenses"):
        set_preference(connection, "DEPAS_CURRENT_HOME", '{"price_clp": 800000}')


def test_an_unknown_setting_is_refused_with_a_suggestion():
    """A misremembered name is the common case, so the error says what was probably meant."""
    with pytest.raises(ValueError, match="DEPAS_TARGET_COST"):
        setting("DEPAS_TARGET_KOST")


def test_a_blank_value_clears_the_setting(connection):
    """Emptying a field is how you turn a preference off, in .env and in the chat alike."""
    set_preference(connection, "DEPAS_ALERT_SECURITY", "24 horas")

    set_preference(connection, "DEPAS_ALERT_SECURITY", "  ")

    assert Preferences.load(connection).value("DEPAS_ALERT_SECURITY") is None


# -- defaults --------------------------------------------------------------------


def test_an_unset_setting_falls_back_to_its_default(connection):
    """Antigüedad is the standing rule, so clearing it moves nothing."""
    prefs = Preferences.load(connection)

    assert prefs.value("DEPAS_TARGET_AGE") == 25
    assert prefs.weights()["cost"] == 1.0
    assert prefs.lease_income("parking") == 0


def test_clearing_a_setting_with_no_default_turns_it_off(connection):
    """Most preferences are opt-in: unset has to mean off, never an invented opinion."""
    set_preference(connection, "DEPAS_TARGET_WALK", "10")
    clear_preference(connection, "DEPAS_TARGET_WALK")

    assert Preferences.load(connection).value("DEPAS_TARGET_WALK") is None


def test_listing_the_settings_says_where_each_value_came_from(connection):
    """`depas config` has to distinguish a deliberate value from a default standing in."""
    set_preference(connection, "DEPAS_TARGET_COST", "850000")
    sources = {declared.name: source for declared, _, source in
               described(Preferences.load(connection))}

    assert sources["DEPAS_TARGET_COST"] == SET
    assert sources["DEPAS_TARGET_AGE"] == DEFAULTED
    assert sources["DEPAS_TARGET_WALK"] == UNSET


# -- what reads them -------------------------------------------------------------


def test_the_alert_filters_are_built_from_the_stored_settings(connection):
    """The watch pass filters on what the database says, not on what the process started with."""
    set_preference(connection, "DEPAS_ALERT_MAX_COST", "900000")
    set_preference(connection, "DEPAS_ALERT_COMMUNES", "nunoa")

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
    set_preference(connection, "DEPAS_ALERT_MAX_COST", "600000")

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


def test_every_documented_setting_is_declared():
    """.env.example is the tour of what can be configured; drift there is a missing knob."""
    declared = {declared.name for declared in SETTINGS}
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    documented = {line.split("=")[0].strip() for line in example.splitlines()
                  if "=" in line and not line.startswith("#")}

    assert documented - set(BOOTSTRAP) <= declared


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
    monkeypatch.setenv("DEPAS_TARGET_COST", "850000")
    monkeypatch.setenv("DEPAS_ALERT_COMMUNES", "nunoa,santiago")

    assert check_environment() == (2, [])


def test_a_value_the_parsers_refuse_is_reported_rather_than_raised(monkeypatch):
    """Every problem at once: a deploy should not be a game of fixing them one per push."""
    monkeypatch.setenv("DEPAS_ALERT_COMMUNES", "nunoa,las condes")
    monkeypatch.setenv("DEPAS_AVAILABLE_BY", "01/11/2026")

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
