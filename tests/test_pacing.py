"""The rate limit is Telegram's, and it is per chat: what waits, and what must not."""
import pytest

from depas import telegram


@pytest.fixture
def waits(monkeypatch):
    """Every wait the pacing asked for, without any of them actually happening."""
    asked = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.time.sleep", lambda seconds: asked.append(seconds))
    monkeypatch.setattr("depas.telegram.call", lambda method, **params: {"message_id": 1})
    # The suite zeroes the intervals; this file is the one place they are the real ones.
    monkeypatch.setattr(telegram, "CROWDED_PACE_SECONDS", 3.0)
    monkeypatch.setattr(telegram, "PRIVATE_PACE_SECONDS", 1.0)
    telegram._LAST_SENT.clear()
    return asked


def test_the_first_message_to_a_chat_waits_for_nothing(waits):
    """A pass posting one card to each of six chats spends no time waiting at all."""
    for chat in ("-1001", "-1002", "-1003"):
        telegram.reply(chat, "hola")

    assert waits == []


def test_a_second_message_to_the_same_chat_waits_out_its_limit(waits):
    """Twenty a minute to a channel is one every three seconds, and that is the wait."""
    telegram.reply("-1001", "una")
    telegram.reply("-1001", "otra")

    assert len(waits) == 1
    assert 0 < waits[0] <= 3.0


def test_a_card_and_its_thread_comment_do_not_queue_behind_each_other(waits):
    """The card is in the channel and the comment in the linked group: two budgets.

    This is the whole point of pacing per chat. A single sleep after every send made a
    channel card wait for a message that never touched the channel.
    """
    telegram.send_listing("-1001", "la tarjeta")
    telegram.reply("-1002", "lo que cambió")  # the discussion group is another chat

    assert waits == []


def test_a_private_chat_is_paced_at_a_message_a_second(waits):
    """A conversation with one reader is not a group, and Telegram says so with the id."""
    telegram.reply("55", "una")
    telegram.reply("55", "otra")

    assert len(waits) == 1
    assert 0 < waits[0] <= 1.0


def test_an_edit_is_paced_like_anything_else(waits):
    """Editing spends the same allowance: Telegram publishes no separate one for it."""
    telegram.edit_text("-1001", 7, "corregido")
    telegram.edit_text("-1001", 8, "corregido")

    assert len(waits) == 1


# ── what Telegram says when we get it wrong ─────────────────────────────────────


def _answers(*payloads):
    """A `requests.post` that hands back each payload in turn."""
    remaining = list(payloads)

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 429 if not payload.get("ok") else 200

        def json(self):
            return self.payload

    return lambda *args, **kwargs: Response(remaining.pop(0))


FLOODED = {"ok": False, "description": "Too Many Requests: retry after 5",
           "parameters": {"retry_after": 5}}


def test_a_429_waits_exactly_as_long_as_telegram_asked(monkeypatch):
    """The wait is a number Telegram gave us, which beats any interval we would pick."""
    slept = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.time.sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr("depas.telegram.requests.post",
                        _answers(FLOODED, {"ok": True, "result": {"message_id": 9}}))

    assert telegram.call("sendMessage", chat_id="-1001", text="hola") == {"message_id": 9}
    # A second over what it asked for: landing on the edge of the window trips it again.
    assert slept == [6]


def test_a_flood_that_will_not_pass_is_raised_rather_than_retried_forever(monkeypatch):
    """Three of these is a rate we are wrong about, not a burst to wait out."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.time.sleep", lambda seconds: None)
    monkeypatch.setattr("depas.telegram.requests.post", _answers(FLOODED, FLOODED, FLOODED))

    with pytest.raises(RuntimeError, match="Too Many Requests"):
        telegram.call("sendMessage", chat_id="-1001", text="hola")


def test_a_failure_that_is_not_a_flood_is_raised_at_once(monkeypatch):
    """A chat the bot was removed from must fail now, not after two pointless waits."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("depas.telegram.time.sleep", lambda seconds: None)
    monkeypatch.setattr("depas.telegram.requests.post", _answers(
        {"ok": False, "description": "Forbidden: bot was kicked", "parameters": {}}))

    with pytest.raises(RuntimeError, match="bot was kicked"):
        telegram.call("sendMessage", chat_id="-1001", text="hola")
