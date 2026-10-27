from datetime import datetime

import pytest

from money_bot import categories
from money_bot.categories import Asker
from money_bot.handlers import TZ

RULES = [
    ("biedronka", "Groceries"),
    ("uber", "Transport"),
    ("uber eats", "Eating out"),
    ("zabka", "Groceries"),
    ("action", "Household"),
    ("mcdonald", "Eating out"),
]
CATS = ["Groceries", "Eating out", "Transport", "Other"]


@pytest.mark.parametrize(
    ("merchant", "expected"),
    [
        ("BIEDRONKA 1234 Poznan", "Groceries"),
        ("Żabka Z5512", "Groceries"),
        ("UBER *TRIP", "Transport"),
        ("Uber Eats Poland", "Eating out"),  # longest pattern wins
        ("Kebab u Aliego", None),
        ("MCDONALDS 0451", "Eating out"),  # a pattern may run into the rest of the word
        ("Card transaction fee", None),  # but must start at a word boundary
    ],
)
def test_match(merchant, expected):
    assert categories.match(merchant, RULES) == expected


def test_fold_handles_polish_letters():
    assert categories.fold("ŁÓDŹ Żółć") == "lodz zolc"


def test_pick_defaults_income_only_without_a_rule():
    assert categories.pick({"merchant": "Salary PIT", "amount_pln": 5000}, RULES) == "Income"
    assert categories.pick({"merchant": "Kebab", "amount_pln": -30}, RULES) is None
    assert categories.pick({"merchant": "uber refund", "amount_pln": "12,50"}, RULES) == "Transport"


def test_starter_rules_are_polish_and_cover_every_category_used():
    patterns = {p for p, _ in categories.starter_rules()}
    assert {"biedronka", "zabka", "uber eats", "rossmann"} <= patterns
    assert not {"atb", "silpo", "allegro", "amazon"} & patterns
    assert {c for _, c in categories.starter_rules()} <= set(categories.DEFAULT_CATEGORIES)


def row(merchant, amount, category=""):
    return {"merchant": merchant, "amount_pln": amount, "category": category}


def test_pending_groups_by_merchant_oldest_first_and_caps():
    rows = [
        row("Kebab", -30),
        row("Lidl", -10, "Groceries"),
        row("Bar Mleczny", -18),
        row("Kebab", -25.5),
        row("Pending", ""),
    ] + [row(f"Shop {i}", -1) for i in range(10)]
    pending = categories.pending(rows)

    assert len(pending) == categories.MAX_QUESTIONS
    assert pending[0][:2] == ("Kebab", 2)
    assert str(pending[0][2]) == "-55.5"
    assert pending[1][0] == "Bar Mleczny"


def test_ask_puts_the_guess_first_and_ends_with_skip():
    asker = Asker(guess=lambda merchants, cats: {"Kebab": "Eating out"})
    (text, keyboard), (_, plain) = asker.ask([row("Kebab", -30), row("Bar", -5)], CATS)

    buttons = [b for line in keyboard for b in line]
    assert text == "Kebab: -30.00 PLN (1 payment). Category?"
    assert buttons[0]["text"] == "★ Eating out"
    assert [b["text"] for b in buttons[1:]] == ["Groceries", "Transport", "Other", "Skip"]
    assert all(len(b["callback_data"]) <= 64 for b in buttons)
    assert all(len(line) <= 2 for line in keyboard)
    assert "★" not in str(plain)


def test_ask_without_guesser_or_with_failing_guesser():
    def broken(merchants, cats):
        raise TimeoutError

    for asker in (Asker(), Asker(guess=broken)):
        ((_, keyboard),) = asker.ask([row("Kebab", -30)], CATS)
        assert keyboard[0][0]["text"] == "Groceries"


class FakeStore:
    def __init__(self):
        self.rules_added = []
        self.recategorized = 0

    def add_rule(self, pattern, category):
        self.rules_added.append((pattern, category))

    def recategorize(self):
        self.recategorized += 1
        return 3


def test_answer_saves_rule_and_fills_old_rows():
    asker = Asker(guess=lambda m, c: {"Kebab U Aliego": "Eating out"})
    ((_, keyboard),) = asker.ask([row("Kebab U Aliego", -30)], CATS)
    store = FakeStore()

    reply = asker.answer(keyboard[0][0]["callback_data"], store)

    assert store.rules_added == [("kebab u aliego", "Eating out")]
    assert store.recategorized == 1
    assert reply == "Kebab U Aliego → Eating out ✓ (3 payments updated)"
    assert (
        asker.answer(keyboard[0][0]["callback_data"], store) == categories.EXPIRED
    )  # one answer per question


def test_answer_skip_and_garbage():
    asker = Asker()
    ((_, keyboard),) = asker.ask([row("Kebab", -30)], CATS)
    store = FakeStore()

    assert asker.answer(keyboard[-1][-1]["callback_data"], store) == "Kebab: skipped"
    for data in ("c:999:0", "c:x:0", "nonsense", ""):
        assert asker.answer(data, store) == categories.EXPIRED
    assert store.rules_added == []


def test_claude_guess_keeps_only_known_merchants_and_categories():
    def ask(prompt, cwd, model, timeout):
        assert "Groceries" in prompt and "Kebab" in prompt
        return {"result": '```json\n{"Kebab": "Eating out", "Lidl": "Food", "Ghost": "Other"}\n```'}

    assert categories.claude_guess(["Kebab", "Lidl"], CATS, ask=ask) == {"Kebab": "Eating out"}


def test_claude_guess_failure_returns_nothing():
    def ask(prompt, cwd, model, timeout):
        raise RuntimeError("claude exited 1")

    assert categories.claude_guess(["Kebab"], CATS, ask=ask) == {}


class StateStore:
    def __init__(self, last=None):
        self.kv = {"ask:last": last} if last else {}

    def state(self, key):
        return self.kv.get(key)


def test_ask_due_after_21_once_a_day():
    evening = datetime(2026, 10, 27, 21, 5, tzinfo=TZ)
    assert categories.ask_due(evening, StateStore()) is True
    assert categories.ask_due(evening, StateStore("2026-10-27")) is False
    assert categories.ask_due(evening.replace(hour=20, minute=59), StateStore()) is False
    assert categories.ask_due(datetime(2026, 10, 28, 0, 30, tzinfo=TZ), StateStore("2026-10-27")) is False
