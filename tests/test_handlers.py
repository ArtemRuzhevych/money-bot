from datetime import datetime
from decimal import Decimal

import pytest

from money_bot import handlers
from money_bot.handlers import TZ, Cash


class FakeStore:
    def __init__(self, rows=(), limit=Decimal(500)):
        self.saved = list(rows)
        self.limit = limit

    def add(self, row):
        if any(r["id"] == row["id"] for r in self.saved):
            return False
        self.saved.append(row)
        return True

    def rows(self):
        return self.saved

    def weekly_limit(self):
        return self.limit

    def set_weekly_limit(self, value):
        self.limit = value

    def categories(self):
        return ["Groceries", "Eating out"]

    def receipt_splits(self):
        return {}


NOW = datetime(2026, 10, 28, 19, 30, tzinfo=TZ)  # a Wednesday


def row(date, amount, merchant="x", category=""):
    return {
        "id": f"{date}{merchant}",
        "date": date,
        "amount_pln": amount,
        "merchant": merchant,
        "category": category,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kebab 32", Cash(Decimal("-32"), "kebab")),
        ("32 kebab", Cash(Decimal("-32"), "kebab")),
        ("kebab 32,50", Cash(Decimal("-32.50"), "kebab")),
        ("coffee with Sam 14.5", Cash(Decimal("-14.5"), "coffee with Sam")),
        ("+200 gift from mum", Cash(Decimal("200"), "gift from mum")),
    ],
)
def test_parse_cash(text, expected):
    assert handlers.parse_cash(text) == expected


@pytest.mark.parametrize("text", ["kebab", "32", "kebab 0", "kebab 12 15", "kebab 3.999", "hello there"])
def test_parse_cash_rejects(text):
    assert handlers.parse_cash(text) is None


def test_week_start_is_monday_midnight_warsaw():
    assert handlers.week_start(NOW) == datetime(2026, 10, 26, 0, 0, tzinfo=TZ)


def test_week_start_across_dst_change():
    # DST ended on Sunday 2026-10-25; the week before started on summer time.
    sunday = datetime(2026, 10, 25, 23, 0, tzinfo=TZ)
    start = handlers.week_start(sunday)
    assert start == datetime(2026, 10, 19, 0, 0, tzinfo=TZ)
    assert start.utcoffset().total_seconds() == 2 * 3600


def test_week_summary_counts_only_this_weeks_spending():
    rows = [
        row("2026-10-26 09:00", -30, "kebab"),
        row("2026-10-27", -20.5, "biedronka"),  # bank rows may carry a date only
        row("2026-10-27 12:00", -15, "kebab"),
        row("2026-10-28 08:00", 1000, "salary"),  # income
        row("2026-10-28 09:00", -300, "to mono", "Transfer"),
        row("2026-10-25 22:00", -99, "last week"),
        row("2026-10-28 10:00", "", "no amount yet"),
    ]
    text = handlers.week_summary(rows, Decimal(500), NOW)
    assert "spent 65.50 of 500.00 PLN" in text
    assert "434.50 left" in text
    assert "kebab 45.00, biedronka 20.50" in text


def test_week_summary_over_budget():
    assert "over by 20.00" in handlers.week_summary([row("2026-10-26", -520)], Decimal(500), NOW)


def test_cash_entry_is_saved_once_and_reports_week():
    store = FakeStore()
    reply = handlers.handle("kebab 32", 7, NOW, store)

    assert store.saved[0]["id"] == "tg-7"
    assert store.saved[0]["amount_pln"] == -32.0
    assert store.saved[0]["account"] == "cash"
    assert store.saved[0]["date"] == "2026-10-28 19:30"
    assert "Logged kebab -32.00 PLN" in reply
    assert "468.00 left" in reply

    assert handlers.handle("kebab 32", 7, NOW, store) == "Already logged."
    assert len(store.saved) == 1


def test_week_command_and_help():
    store = FakeStore([row("2026-10-27", -10, "bus")])
    assert "spent 10.00" in handlers.handle("/week", 1, NOW, store)
    assert "kebab 32" in handlers.handle("/start", 2, NOW, store)
    assert "`" not in handlers.HELP  # Telegram shows backticks literally in plain text
    assert "kebab 32" in handlers.handle("what?", 3, NOW, store)


def update(user_id, text="kebab 32"):
    return {
        "update_id": 1,
        "message": {"message_id": 5, "from": {"id": user_id}, "chat": {"id": 99}, "text": text},
    }


def callback(user_id, data="c:1:0"):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 77, "chat": {"id": 99}},
        },
    }


class FakeAsker:
    def __init__(self, questions=()):
        self.questions = list(questions)
        self.answered = []

    def ask(self, rows, cats):
        return self.questions

    def answer(self, data, store):
        self.answered.append(data)
        return "Kebab → Eating out ✓ (1 payments updated)"


class FakeDesk:
    def __init__(self):
        self.photos = []
        self.answered = []

    def from_photo(self, file_id, store, now):
        self.photos.append(file_id)
        return "Biedronka, 24 Oct, 47.13 PLN, 6 items.", [
            [{"text": "Cash", "callback_data": "r:rcpt-1:cash"}]
        ]

    def answer(self, data, store):
        self.answered.append(data)
        return "Biedronka 47.13 PLN saved as cash ✓"


def run(upd, asker=None, store=None, desk=None):
    return handlers.process(
        upd,
        allowed_id=42,
        store=store or FakeStore(),
        now=NOW,
        asker=asker or FakeAsker(),
        desk=desk or FakeDesk(),
    )


def test_process_ignores_other_users():
    assert run(update(666)) is None
    assert run(callback(666)) is None


def test_process_replies_to_owner():
    (reply,) = run(update(42))
    assert reply.chat_id == 99
    assert reply.text.startswith("Logged kebab")
    assert reply.keyboard is None


def test_process_non_text_message():
    upd = update(42)
    del upd["message"]["text"]
    assert run(upd)[0].text == "I can read text and receipt photos."


def test_process_skips_non_message_updates():
    assert run({"update_id": 3, "edited_message": {}}) is None


def test_callback_answer_edits_the_question():
    asker = FakeAsker()
    (reply,) = run(callback(42, "c:4:2"), asker)

    assert asker.answered == ["c:4:2"]
    assert reply.edit_message_id == 77
    assert reply.callback_id == "cb1"
    assert reply.text.startswith("Kebab → Eating out")


def test_categorize_sends_one_message_per_question():
    keyboard = [[{"text": "Groceries", "callback_data": "c:1:0"}]]
    replies = run(update(42, "/categorize"), FakeAsker([("Kebab?", keyboard), ("Bar?", keyboard)]))

    assert [r.text for r in replies] == ["Kebab?", "Bar?"]
    assert replies[0].keyboard == keyboard


def test_categorize_with_nothing_new():
    assert run(update(42, "/categorize"))[0].text == "Nothing new to categorise."


def test_week_summary_shows_top_categories():
    rows = [
        {**row("2026-10-26", -30, "kebab"), "category": "Eating out"},
        {**row("2026-10-27", -50, "lidl"), "category": "Groceries"},
        {**row("2026-10-27", -12, "bar"), "category": "Eating out"},
        row("2026-10-27", -7, "mystery"),
    ]
    text = handlers.week_summary(rows, Decimal(500), NOW)
    assert "By category: Groceries 50.00, Eating out 42.00, Uncategorised 7.00" in text


def test_limit_shows_current_limit():
    reply = handlers.handle("/limit", 1, NOW, FakeStore(limit=Decimal(450)))
    assert "Weekly limit is 450.00 PLN" in reply
    assert "/limit 600" in reply


@pytest.mark.parametrize(("arg", "expected"), [("600", Decimal(600)), ("650,50", Decimal("650.50"))])
def test_limit_sets_new_limit_and_reports_week(arg, expected):
    store = FakeStore([row("2026-10-27", -100)])
    reply = handlers.handle(f"/limit {arg}", 1, NOW, store)

    assert store.limit == expected
    assert f"Weekly limit set to {expected:.2f} PLN" in reply
    assert f"spent 100.00 of {expected:.2f} PLN" in reply


@pytest.mark.parametrize("arg", ["0", "-5", "abc", "100001", "12 13"])
def test_limit_rejects_bad_values(arg):
    store = FakeStore()
    reply = handlers.handle(f"/limit {arg}", 1, NOW, store)
    assert store.limit == Decimal(500)
    assert reply.startswith("Limit must be")


def test_photo_goes_to_the_receipt_desk_largest_size():
    upd = update(42)
    del upd["message"]["text"]
    upd["message"]["photo"] = [{"file_id": "small"}, {"file_id": "large"}]
    desk = FakeDesk()

    (reply,) = run(upd, desk=desk)
    assert desk.photos == ["large"]
    assert reply.keyboard[0][0]["text"] == "Cash"


def test_image_file_goes_to_the_desk_other_files_dont():
    upd = update(42)
    del upd["message"]["text"]
    upd["message"]["document"] = {"file_id": "doc1", "mime_type": "image/jpeg"}
    desk = FakeDesk()
    run(upd, desk=desk)
    assert desk.photos == ["doc1"]

    upd["message"]["document"] = {"file_id": "doc2", "mime_type": "application/pdf"}
    assert run(upd, desk=desk)[0].text == "I can read text and receipt photos."


def test_receipt_buttons_go_to_the_desk_category_buttons_to_the_asker():
    desk, asker = FakeDesk(), FakeAsker()
    (reply,) = run(callback(42, "r:rcpt-1:cash"), asker, desk=desk)
    run(callback(42, "c:1:0"), asker, desk=desk)

    assert desk.answered == ["r:rcpt-1:cash"]
    assert asker.answered == ["c:1:0"]
    assert reply.edit_message_id == 77


def test_week_counts_a_receipt_payment_by_its_item_shares():
    rows = [{**row("2026-10-27", -50, "biedronka"), "category": "Groceries", "receipt_id": "rcpt-1"}]
    splits = {"rcpt-1": {"Groceries": Decimal("30"), "Fun": Decimal("10")}}  # items say 40, bank says 50
    text = handlers.week_summary(rows, Decimal(500), NOW, splits)
    assert "By category: Groceries 37.50, Fun 12.50" in text


class FakeOracle:
    def __init__(self):
        self.asked = []

    def answer(self, chat_id, question, store, now):
        self.asked.append((chat_id, question))
        return "You spent 42.00 PLN."


def test_ask_goes_to_the_oracle():
    oracle = FakeOracle()
    upd = update(42, "/ask how much on  groceries?")
    (reply,) = handlers.process(upd, 42, FakeStore(), NOW, FakeAsker(), FakeDesk(), oracle)

    assert oracle.asked == [(99, "how much on  groceries?")]
    assert reply.text == "You spent 42.00 PLN."


def test_ask_without_a_question_or_claude():
    oracle = FakeOracle()
    (empty,) = handlers.process(update(42, "/ask"), 42, FakeStore(), NOW, FakeAsker(), FakeDesk(), oracle)
    (off,) = run(update(42, "/ask anything?"))

    assert oracle.asked == []
    assert empty.text == handlers.ASK_EXAMPLES
    assert off.text == handlers.NO_CLAUDE_ASK
    assert "/ask" in handlers.HELP


@pytest.mark.parametrize(
    ("text", "merchant"),
    [("blik 50 allegro", "allegro"), ("allegro 50 BLIK", "allegro"), ("blik 50", "BLIK")],
)
def test_blik_entries_go_to_the_blik_account(text, merchant, monkeypatch):
    monkeypatch.setenv("BLIK_ACCOUNT", "millennium")
    store = FakeStore()
    reply = handlers.handle(text, 9, NOW, store)

    (saved,) = store.saved
    assert (saved["account"], saved["merchant"], saved["amount"]) == ("millennium", merchant, -50.0)
    assert reply.startswith(f"Logged {merchant} -50.00 PLN (millennium).")


def test_blik_account_defaults_to_blik(monkeypatch):
    monkeypatch.delenv("BLIK_ACCOUNT", raising=False)
    store = FakeStore()
    handlers.handle("blik 50 allegro", 9, NOW, store)
    assert store.saved[0]["account"] == "blik"


def test_blik_inside_a_word_stays_cash():
    store = FakeStore()
    handlers.handle("bliks shop 10", 9, NOW, store)
    assert store.saved[0]["account"] == "cash"
