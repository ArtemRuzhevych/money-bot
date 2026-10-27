import pytest
import requests

from money_bot.telegram import Telegram

TOKEN = "123:secret-token"


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, timeout):
        self.calls.append((url, None, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


def test_updates_advance_offset():
    session = FakeSession(
        {"ok": True, "result": [{"update_id": 10}, {"update_id": 11}]},
        {"ok": True, "result": []},
    )
    bot = Telegram(TOKEN, session)

    assert [u["update_id"] for u in bot.updates()] == [10, 11]
    assert bot.updates() == []

    url, params, timeout = session.calls[1]
    assert url == f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    assert params["offset"] == 12
    assert timeout > params["timeout"]


def test_send_message():
    session = FakeSession({"ok": True, "result": {}})
    Telegram(TOKEN, session).send(99, "hi")
    assert session.calls[0][0].endswith("/sendMessage")
    assert session.calls[0][1] == {"chat_id": 99, "text": "hi"}


def test_api_error_is_raised():
    bot = Telegram(TOKEN, FakeSession({"ok": False, "description": "Unauthorized"}))
    with pytest.raises(RuntimeError, match="Unauthorized"):
        bot.updates()


def test_network_errors_never_leak_the_token():
    err = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/getUpdates")
    bot = Telegram(TOKEN, FakeSession(err))
    with pytest.raises(RuntimeError) as exc:
        bot.updates()
    assert "secret-token" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert bot.offset == 0


def test_polls_messages_and_button_presses():
    session = FakeSession({"ok": True, "result": []})
    Telegram(TOKEN, session).updates()
    assert session.calls[0][1]["allowed_updates"] == ["message", "callback_query"]


def test_send_with_inline_keyboard():
    session = FakeSession({"ok": True, "result": {}})
    keyboard = [[{"text": "Groceries", "callback_data": "c:1:0"}]]
    Telegram(TOKEN, session).send(99, "Kebab?", keyboard)
    assert session.calls[0][1] == {
        "chat_id": 99,
        "text": "Kebab?",
        "reply_markup": {"inline_keyboard": keyboard},
    }


def test_answer_callback_and_edit_message():
    session = FakeSession({"ok": True, "result": True}, {"ok": True, "result": {}})
    bot = Telegram(TOKEN, session)
    bot.answer_callback("cb1")
    bot.edit(99, 77, "Kebab → Eating out ✓")

    assert session.calls[0][0].endswith("/answerCallbackQuery")
    assert session.calls[0][1] == {"callback_query_id": "cb1"}
    assert session.calls[1][0].endswith("/editMessageText")
    assert session.calls[1][1] == {"chat_id": 99, "message_id": 77, "text": "Kebab → Eating out ✓"}


class FileResponse:
    status_code = 200
    content = b"jpeg-bytes"


def test_download_returns_bytes_and_suffix():
    session = FakeSession({"ok": True, "result": {"file_path": "photos/file_7.jpg"}}, FileResponse())
    assert Telegram(TOKEN, session).download("abc") == (b"jpeg-bytes", ".jpg")
    assert session.calls[0][1] == {"file_id": "abc"}
    assert session.calls[1][0] == f"https://api.telegram.org/file/bot{TOKEN}/photos/file_7.jpg"


def test_download_errors_never_leak_the_token():
    err = requests.ConnectionError(f"Max retries exceeded with url: /file/bot{TOKEN}/photos/x.jpg")
    session = FakeSession({"ok": True, "result": {"file_path": "photos/x.jpg"}}, err)
    with pytest.raises(RuntimeError) as exc:
        Telegram(TOKEN, session).download("abc")
    assert "secret-token" not in str(exc.value)
