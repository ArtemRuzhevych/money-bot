import logging
from datetime import datetime

import pytest

from money_bot import __main__ as app
from money_bot.categories import Asker
from money_bot.handlers import TZ
from money_bot.receipts import ReceiptDesk

DESK = ReceiptDesk(download=None, ask=None)


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.edits = []
        self.answered = []
        self.fail = fail

    def send(self, chat_id, text, keyboard=None):
        if self.fail:
            raise RuntimeError("telegram sendMessage: ConnectionError")
        self.sent.append((chat_id, text))

    def edit(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    def answer_callback(self, callback_id):
        self.answered.append(callback_id)

    def download(self, file_id):
        return b"jpeg", ".jpg"


class BrokenStore:
    def add(self, row):
        raise OSError("Sheets API down")


def update(user_id, text="kebab 32", update_id=1):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "from": {"id": user_id}, "chat": {"id": 99}, "text": text},
    }


def test_handler_crash_replies_and_keeps_going(caplog):
    bot = FakeBot()
    app.handle_updates(bot, [update(42), update(42, "/start", 2)], BrokenStore(), 42, set(), Asker(), DESK)

    assert bot.sent[0] == (99, "Something went wrong, it's in the log.")
    assert "kebab 32" in bot.sent[1][1]  # the next update still gets answered
    assert "handling update 1 failed" in caplog.text


def test_strangers_get_no_reply_and_are_logged_once(caplog):
    bot = FakeBot()
    ignored: set = set()
    with caplog.at_level(logging.WARNING):
        app.handle_updates(
            bot, [update(666), update(666, update_id=2)], BrokenStore(), 42, ignored, Asker(), DESK
        )

    assert bot.sent == []
    assert ignored == {666}
    assert caplog.text.count("ignoring messages from user 666") == 1


def test_failed_reply_is_logged_not_raised(caplog):
    app.handle_updates(FakeBot(fail=True), [update(42, "/start")], BrokenStore(), 42, set(), Asker(), DESK)
    assert "reply failed" in caplog.text


def test_missing_config_idles_with_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    for key in app.REQUIRED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHEET_ID", "x")

    def stop(reason):
        raise SystemExit(reason)

    monkeypatch.setattr(app, "idle", stop)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, GOOGLE_SERVICE_ACCOUNT_FILE"):
        app.main()
    for handler in logging.root.handlers[:]:  # main() attached a file handler under tmp_path
        handler.close()
        logging.root.removeHandler(handler)


def test_monobank_sync_is_off_without_token(monkeypatch, caplog):
    monkeypatch.delenv("MONOBANK_TOKEN", raising=False)
    with caplog.at_level(logging.INFO):
        assert app.make_mono(store=None) is None
    assert "monobank sync is off" in caplog.text


def test_monobank_sync_is_on_with_token(monkeypatch):
    monkeypatch.setenv("MONOBANK_TOKEN", "tok")
    assert app.make_mono(store=None).api.token == "tok"


def test_button_press_is_acknowledged_and_edits_the_question():
    bot = FakeBot()
    press = {
        "update_id": 5,
        "callback_query": {
            "id": "cb9",
            "from": {"id": 42},
            "data": "c:1:0",
            "message": {"message_id": 7, "chat": {"id": 99}},
        },
    }
    app.handle_updates(bot, [press], BrokenStore(), 42, set(), Asker(), DESK)

    assert bot.answered == ["cb9"]
    assert bot.edits == [(99, 7, "This question expired, send /categorize")]
    assert bot.sent == []


class AskStore:
    def __init__(self):
        self.kv = {}

    def state(self, key):
        return self.kv.get(key)

    def set_state(self, key, value):
        self.kv[key] = value

    def rows(self):
        return [{"date": "2026-10-26", "merchant": "Bar Mleczny", "amount_pln": -18, "category": ""}]

    def categories(self):
        return ["Eating out", "Other"]

    def weekly_limit(self):
        return 500

    def receipt_splits(self):
        return {}


def test_daily_questions_go_out_once_after_21():
    bot, store = FakeBot(), AskStore()
    app.maybe_ask(bot, store, Asker(), 42, datetime(2026, 10, 27, 20, 0, tzinfo=TZ))
    assert bot.sent == []

    evening = datetime(2026, 10, 27, 21, 0, tzinfo=TZ)
    app.maybe_ask(bot, store, Asker(), 42, evening)
    app.maybe_ask(bot, store, Asker(), 42, evening)

    assert bot.sent == [(42, "Bar Mleczny: -18.00 PLN (1 payment). Category?")]  # quiet day: no digest
    assert store.kv["ask:last"] == "2026-10-27"


def test_digest_goes_out_before_the_questions():
    bot = FakeBot()
    app.maybe_ask(bot, AskStore(), Asker(), 42, datetime(2026, 10, 26, 21, 0, tzinfo=TZ))
    assert bot.sent[0][1].splitlines() == [
        "Today: 1 payment, 18.00 PLN (Bar Mleczny 18.00).",
        "This week: spent 18.00 of 500.00 PLN, 482.00 left.",
    ]
    assert bot.sent[1][1].startswith("Bar Mleczny")


def test_sunday_summary_goes_out_once():
    bot, store = FakeBot(), AskStore()
    sunday = datetime(2026, 11, 1, 20, 30, tzinfo=TZ)
    app.maybe_summarize(bot, store, 42, sunday, ask=None)
    app.maybe_summarize(bot, store, 42, sunday, ask=None)

    assert len(bot.sent) == 1
    assert bot.sent[0][1].startswith("Week from Mon 26 Oct: spent 18.00")
    assert store.kv["summary:last"] == "2026-10-26"


def test_ask_needs_claude():
    assert app.make_oracle(FakeBot(), False) is None
    assert app.make_oracle(FakeBot(), True).ask is not None


def test_no_claude_token_means_questions_without_guesses(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert app.claude_ready() is False
    assert app.make_asker(False).guess is None
    assert app.make_desk(FakeBot(), False).ask is None


class InboxStore:
    def __init__(self, items):
        self.items, self.saved = list(items), []

    def inbox_rows(self):
        return list(self.items)

    def clear_inbox(self, count):
        del self.items[:count]

    def add(self, row):
        self.saved.append(row)
        return True


class PLN:
    def to_pln(self, amount, code, day):
        return amount


def test_wallet_payments_are_saved_and_unreadable_ones_reported():
    bot = FakeBot()
    store = InboxStore(
        [
            {
                "card": "Millennium",
                "merchant": "Biedronka",
                "amount": "47,13 zł",
                "time": "2026-10-27T18:03:00+01:00",
            },
            {"card": "Millennium", "merchant": "Kiosk", "amount": "?", "time": ""},
        ]
    )
    app.pull_wallet(bot, store, PLN(), 42)

    assert [r["merchant"] for r in store.saved] == ["Biedronka"]
    assert bot.sent == [
        (42, "Couldn't read an Apple Pay payment: Kiosk ? (Millennium). Type it in, like: kiosk 12")
    ]
