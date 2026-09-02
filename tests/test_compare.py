import json

import pytest

from depas.bot import NO_HOME, _handle
from depas.models import Listing
from depas.store import connect, remember_card, save, save_detail
from tests.support import prefs

HOME = {
    "commune": "nunoa", "price_clp": 800_000, "common_expenses": 130_000,
    "area_m2": 62, "bedrooms": 2, "bathrooms": 1, "floor": 3, "age_years": 30,
    "lat": -33.45590, "lon": -70.59780, "parking_spaces": 1, "has_elevator": True,
    "has_terrace": True,
}
CHANNEL, CARD, GROUP, THREAD = -1001, 77, -1002, 88


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DEPAS_LOCATIONS", "pega,-33.41720,-70.60600")
    monkeypatch.setenv("DEPAS_PARKING_INCOME", "150000")
    # The router is somebody else's server; the offline estimate is what a test may use.
    monkeypatch.setattr("depas.commute.routed_minutes", lambda *a: None)
    connection = connect(tmp_path / "test.db")
    save(connection, [Listing(portal="portalinmobiliario", external_id="MLC-1",
                              url="https://portalinmobiliario.com/MLC-1-x-_JM",
                              price=700_000, currency="CLP", price_clp=700_000,
                              commune="santiago", area_m2=50.0, bedrooms=3)])
    save_detail(connection, "portalinmobiliario", "MLC-1", {
        "common_expenses": 90_000, "area_useful_m2": 50.0, "floor": 8, "age_years": 4,
        "bathrooms": 2, "has_elevator": 1, "has_pool": 1,
        "lat": -33.44500, "lon": -70.65400, "nearest_station": "Santa Ana",
        "walk_minutes": 6, "commute": json.dumps({"pega": 31}),
    })
    remember_card(connection, CHANNEL, CARD, "portalinmobiliario", "MLC-1")
    return connection


@pytest.fixture
def answered(monkeypatch):
    """Every plain reply the bot posts."""
    said = []
    monkeypatch.setattr("depas.bot.reply",
                        lambda chat, text, thread=None, reply_to=None: said.append(text))
    return said


def _comment(text="/compare"):
    return {"chat": {"id": CHANNEL}, "message_id": 900,
            "reply_to_message": {"message_id": CARD, "text": "🟢 B 80 ✔️"},
            "from": {"username": "vicente"}, "text": text}


@pytest.fixture
def at_home(monkeypatch):
    monkeypatch.setenv("DEPAS_CURRENT_HOME", json.dumps(HOME))


def test_a_compare_with_no_home_configured_says_so(connection, answered):
    """Without DEPAS_CURRENT_HOME there is nothing to compare against."""
    _handle(connection, None, _comment(), prefs())

    assert answered == [NO_HOME]


@pytest.mark.parametrize(
    ("expected", "reason"),
    [
        ("💰 neto al mes: $780.000 → $790.000 · 🔺 $10.000 peor",
         "sublet income nets the home down"),
        ("📐 superficie: 62 m² → 50 m² · 🔻 12 m² peor", "smaller is worse"),
        ("🛏️ dormitorios: 2 → 3 · 🔺 1 mejor", "more bedrooms is better"),
        ("🏗️ antigüedad: 30 años → 4 años · 🔻 26 años mejor", "newer is better"),
        ("🚇 Chile España (L3) → Santa Ana (L2/L5)", "the station and its lines change"),
        ("🧭 pega: 27 min → 31 min · 🔺 4 min peor", "every configured trip is compared"),
        ("✨ gana: piscina", "an amenity the listing adds"),
        ("👎 pierde: terraza", "an amenity the listing drops"),
    ],
)
def test_every_axis_is_compared_against_your_own_place(connection, answered, at_home,
                                                       expected, reason):
    """/compare answers with the listing set against your apartment, figure by figure."""
    _handle(connection, None, _comment(), prefs())

    assert expected in answered[0], reason


def test_the_answer_grades_both_places(connection, answered, at_home):
    """The point of comparing is the verdict, so both grades open the card."""
    _handle(connection, None, _comment(), prefs())

    assert "⚖️ <b>Tu depto → este aviso</b>" in answered[0]
    assert answered[0].splitlines()[1].count("→") == 1


def test_the_command_is_recognised_when_addressed_to_the_bot(connection, answered, at_home):
    """Telegram appends @thebot whenever more than one bot shares the chat."""
    _handle(connection, None, _comment("/compare@depas_bot"), prefs())

    assert "📐 superficie" in answered[0]


def test_the_home_json_replaces_the_separate_current_cost(at_home):
    """One secret describes the place, so its net cost need not be configured twice."""
    assert prefs().current_cost() == 800_000 + 130_000 - 0


def test_an_explicit_current_cost_still_wins(at_home, monkeypatch):
    """DEPAS_CURRENT_COST stays the override for anyone who set it before."""
    monkeypatch.setenv("DEPAS_CURRENT_COST", "999000")

    assert prefs().current_cost() == 999_000


def test_a_home_missing_a_required_figure_is_refused(monkeypatch):
    """A half-filled secret must fail loudly rather than compare against zero."""
    monkeypatch.setenv("DEPAS_CURRENT_HOME", json.dumps({"price_clp": 800_000}))

    with pytest.raises(ValueError, match="common_expenses"):
        prefs().current_cost()
