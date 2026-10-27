import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from money_bot import insights
from money_bot.handlers import TZ

WED = datetime(2026, 10, 28, 21, 5, tzinfo=TZ)
SUN = datetime(2026, 11, 1, 20, 0, tzinfo=TZ)


def row(date, amount, merchant="x", category="", receipt_id=""):
    return {
        "id": f"{date}{merchant}{amount}",
        "date": date,
        "amount_pln": amount,
        "account": "mono",
        "merchant": merchant,
        "description": "secret description",
        "category": category,
        "receipt_id": receipt_id,
        "note": "secret note",
    }


class Store:
    def __init__(self, rows=(), items=()):
        self.kv = {}
        self._rows = list(rows)
        self._items = list(items)

    def state(self, key):
        return self.kv.get(key)

    def set_state(self, key, value):
        self.kv[key] = value

    def rows(self):
        return self._rows

    def receipt_items(self):
        return self._items

    def weekly_limit(self):
        return Decimal(500)

    def categories(self):
        return ["Groceries", "Transport"]


# Daily digest


def test_digest_lists_todays_payments_income_and_week():
    rows = [
        row("2026-10-28 12:00", -47.13, "Biedronka", "Groceries"),
        row("2026-10-28", -30, "Orlen", "Transport"),
        row("2026-10-28", -7.07, "Żabka"),
        row("2026-10-28", -1, "Kiosk"),
        row("2026-10-28", 2000, "Salary", "Income"),
        row("2026-10-28", -100, "To savings", "Transfer"),
        row("2026-10-27", -20, "Bus"),
    ]
    text = insights.digest(rows, Decimal(500), WED)
    assert text.splitlines() == [
        "Today: 4 payments, 85.20 PLN (Biedronka 47.13, Orlen 30.00, Żabka 7.07).",
        "Today in: +2000.00 PLN.",
        "This week: spent 105.20 of 500.00 PLN, 394.80 left.",
    ]


def test_no_digest_on_a_quiet_day():
    rows = [row("2026-10-27", -20, "Bus"), row("2026-10-28", -100, "To savings", "Transfer")]
    assert insights.digest(rows, Decimal(500), WED) is None


def test_income_only_day_still_gets_a_digest():
    text = insights.digest([row("2026-10-28", 50, "Gift")], Decimal(500), WED)
    assert text.splitlines()[0] == "Today in: +50.00 PLN."


# Sunday summary


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (datetime(2026, 11, 1, 19, 59, tzinfo=TZ), False),
        (SUN, True),
        (datetime(2026, 11, 1, 23, 59, tzinfo=TZ), True),
        (datetime(2026, 11, 2, 20, 0, tzinfo=TZ), False),  # Monday
        (datetime(2026, 10, 31, 21, 0, tzinfo=TZ), False),  # Saturday
    ],
)
def test_summary_due_only_sunday_evening(now, due):
    assert insights.summary_due(now, Store()) is due


def test_summary_due_once_per_week():
    store = Store()
    store.set_state("summary:last", "2026-10-26")
    assert insights.summary_due(SUN, store) is False
    assert insights.summary_due(datetime(2026, 11, 8, 20, 0, tzinfo=TZ), store) is True


def test_summary_compares_with_last_week():
    rows = [
        row("2026-10-27", -200, "Biedronka", "Groceries"),
        row("2026-10-30", -100, "Pizza Hut", "Eating out"),
        row("2026-11-01", -36, "Bolt", "Transport"),
        row("2026-10-31", -20, "Kiosk"),
        row("2026-10-20", -180, "Lidl", "Groceries"),
        row("2026-10-21", -120, "Bolt", "Transport"),
        row("2026-10-28", 3000, "Salary", "Income"),
    ]
    text = insights.summary(rows, Decimal(500), SUN)
    assert text.splitlines() == [
        "Week from Mon 26 Oct: spent 356.00 of 500.00 PLN, 144.00 left.",
        "Last week 300.00, so +19% this week.",
        "Categories: Groceries 200.00 (+20.00), Eating out 100.00 (new), Transport 36.00 (-84.00).",
        "Shops: Biedronka 200.00, Pizza Hut 100.00, Bolt 36.00.",
        "1 payment has no category, send /categorize.",
    ]


def test_summary_of_an_empty_week():
    text = insights.summary([row("2026-10-20", -50, "Lidl", "Groceries")], Decimal(500), SUN)
    assert text.splitlines() == [
        "Week from Mon 26 Oct: nothing spent this week.",
        "Last week 50.00.",
    ]


def test_summary_remark_sees_only_the_numbers():
    calls = []

    def ask(prompt, cwd, model, timeout):
        calls.append((prompt, Path(cwd), model))
        assert list(Path(cwd).iterdir()) == []  # nothing for Read to find
        return {"result": "  Groceries went up a bit.\n"}

    rows = [row("2026-10-27", -42, "Biedronka", "Groceries")]
    text = insights.summary(rows, Decimal(500), SUN, ask=ask)
    (prompt, folder, model) = calls[0]
    assert model == "sonnet"
    assert "Biedronka 42.00" in prompt and "secret" not in prompt
    assert text.endswith("\n\nGroceries went up a bit.")
    assert not folder.exists()


def test_summary_goes_out_without_remark_when_claude_fails():
    def ask(prompt, cwd, model, timeout):
        raise RuntimeError("claude exited 1")

    text = insights.summary([row("2026-10-27", -42, "Biedronka", "Groceries")], Decimal(500), SUN, ask=ask)
    assert text.startswith("Week from Mon 26 Oct: spent 42.00")
    assert "\n\n" not in text


# /ask


def test_snapshot_keeps_a_year_and_drops_private_columns(tmp_path):
    rows = [
        row("2026-10-27", -42, "Biedronka", "Groceries", "rcpt-1"),
        row("2025-10-27", -10, "Old shop"),
        row("2025-10-29", -11, "Year ago"),
    ]
    items = [
        {"receipt_id": "rcpt-1", "item": "Mleko", "qty": 1, "price": 4.2, "category": "Groceries"},
        {"receipt_id": "rcpt-0", "item": "Old item", "qty": 1, "price": 1, "category": "Other"},
    ]
    insights.snapshot(tmp_path, rows, items, WED.date())

    with open(tmp_path / "transactions.csv", encoding="utf-8") as f:
        saved = list(csv.DictReader(f))
    assert [r["merchant"] for r in saved] == ["Biedronka", "Year ago"]
    assert list(saved[0]) == ["date", "amount_pln", "account", "merchant", "category", "receipt_id"]
    with open(tmp_path / "receipt_items.csv", encoding="utf-8") as f:
        assert [r["item"] for r in csv.DictReader(f)] == ["Mleko"]


def test_ask_sends_looking_then_answers_from_the_snapshot():
    sent, calls = [], []

    def ask(prompt, cwd, model, timeout):
        files = sorted(p.name for p in Path(cwd).iterdir())
        calls.append((prompt, Path(cwd), model, timeout, files))
        return {"result": "You spent 42.00 PLN on groceries.\n"}

    oracle = insights.Oracle(send=lambda chat, text: sent.append((chat, text)), ask=ask)
    store = Store([row("2026-10-27", -42, "Biedronka", "Groceries")])
    answer = oracle.answer(99, "how much on groceries?", store, WED)

    assert sent == [(99, insights.LOOKING)]
    assert answer == "You spent 42.00 PLN on groceries."
    prompt, folder, model, timeout, files = calls[0]
    assert (model, timeout) == ("sonnet", 180)
    assert files == ["receipt_items.csv", "transactions.csv"]
    for part in ["how much on groceries?", "2026-10-28", "500.00", '"Groceries"', "Transfer", "investment"]:
        assert part in prompt
    assert not folder.exists()


def test_ask_failure_is_reported():
    def ask(prompt, cwd, model, timeout):
        raise TimeoutError

    oracle = insights.Oracle(send=lambda chat, text: None, ask=ask)
    assert oracle.answer(99, "q?", Store(), WED) == insights.FAILED
