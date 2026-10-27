import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from money_bot import receipts
from money_bot.handlers import TZ
from money_bot.receipts import NotAReceipt, Receipt, ReceiptDesk

CATS = ["Groceries", "Household", "Fun", "Other"]
NOW = datetime(2026, 10, 24, 19, 0, tzinfo=TZ)
BIEDRONKA = {
    "shop": "Biedronka",
    "date": "2026-10-24",
    "currency": "PLN",
    "total": 47.13,
    "items": [
        {"name": "Mleko 2% 1L", "qty": 1, "price": 3.49, "category": "Groceries"},
        {"name": "Chleb pszenny", "qty": 1, "price": 4.99, "category": "Groceries"},
        {"name": "Jajka L 10szt", "qty": 1, "price": 11.99, "category": "Groceries"},
        {"name": "Banany", "qty": 1.2, "price": 7.19, "category": "Groceries"},
        {"name": "Pasta do zębów", "qty": 1, "price": 8.49, "category": "Household"},
        {"name": "Red Bull 0,25", "qty": 2, "price": 10.98, "category": "Energy drinks"},
    ],
}


def fake_ask(reply: dict, seen: list | None = None):
    def ask(prompt, cwd, model, timeout):
        files = sorted(p.name for p in Path(cwd).iterdir())
        if seen is not None:
            seen.append((prompt, files, cwd, model))
        return {"result": "```json\n" + json.dumps(reply) + "\n```"}

    return ask


def test_read_puts_only_the_image_in_a_temp_folder_and_cleans_up():
    seen: list = []
    receipt = receipts.read(b"\xff\xd8jpeg", ".jpg", CATS, NOW.date(), ask=fake_ask(BIEDRONKA, seen))

    prompt, files, cwd, model = seen[0]
    assert files == ["receipt.jpg"]
    assert model == "haiku"
    assert '"Groceries"' in prompt and "receipt.jpg" in prompt
    assert not Path(cwd).exists()
    assert receipt.shop == "Biedronka"
    assert receipt.total == Decimal("47.13")
    assert receipt.items[-1]["category"] == "Other"  # off-list category
    assert receipt.id.startswith("rcpt-")


def test_read_cleans_up_when_claude_fails():
    seen: list = []

    def broken(prompt, cwd, model, timeout):
        seen.append(cwd)
        raise RuntimeError("claude exited 1")

    with pytest.raises(RuntimeError):
        receipts.read(b"img", ".png", CATS, NOW.date(), ask=broken)
    assert not Path(seen[0]).exists()


def test_not_a_receipt():
    with pytest.raises(NotAReceipt):
        receipts.read(b"img", ".jpg", CATS, NOW.date(), ask=fake_ask({"error": "not a receipt"}))


def test_missing_date_falls_back_to_today_and_id_is_stable():
    data = {**BIEDRONKA, "date": "unknown"}
    a = receipts.read(b"1", ".jpg", CATS, date(2026, 10, 24), ask=fake_ask(data))
    b = receipts.read(b"2", ".jpg", CATS, date(2026, 10, 24), ask=fake_ask(BIEDRONKA))
    assert a.date == "2026-10-24"
    assert a.id == b.id  # same shop, day and total: same receipt


def make(total="47.13", items=None, day="2026-10-24"):
    items = items or [
        {"name": "food", "qty": 1, "price": Decimal("36.15"), "category": "Groceries"},
        {"name": "drinks", "qty": 2, "price": Decimal("10.98"), "category": "Fun"},
    ]
    return Receipt("rcpt-1", day, "Biedronka", Decimal(total), "PLN", items)


def test_split_and_problems():
    r = make()
    assert receipts.split(r) == {"Groceries": Decimal("36.15"), "Fun": Decimal("10.98")}
    assert receipts.main_category(r) == "Groceries"
    assert receipts.problems(r) == []
    assert receipts.problems(make(total="50.00")) == ["items add up to 47.13, not 50.00"]


def tx(id_, day, amount, receipt_id=""):
    return {
        "id": id_,
        "date": day,
        "amount_pln": amount,
        "receipt_id": receipt_id,
        "account": "mono PLN *1111",
    }


def test_match_same_or_next_day_unlinked_closest_first():
    rows = [
        tx("a", "2026-10-23 10:00", -47.13),  # day before: no
        tx("b", "2026-10-25 09:00", -47.13),  # next day: yes, but later
        tx("c", "2026-10-24 18:00", -47.13, "rcpt-old"),  # already has a receipt
        tx("d", "2026-10-24 18:30", "-47,13"),  # same day: best
        tx("e", "2026-10-24 18:40", -47.10),  # wrong amount
    ]
    row_number, row = receipts.find_match(rows, make())
    assert row["id"] == "d"
    assert row_number == 5  # sheet row: header + 4

    assert receipts.find_match(rows[:3], make())[1]["id"] == "b"
    assert receipts.find_match([rows[0], rows[2], rows[4]], make()) is None


class FakeStore:
    def __init__(self, rows=()):
        self.tx = list(rows)
        self.saved_receipts: list = []
        self.links: list = []
        self.added: list = []

    def categories(self):
        return CATS

    def rows(self):
        return self.tx

    def has_receipt(self, receipt_id):
        return any(r.id == receipt_id for r, _, _ in self.saved_receipts)

    def save_receipt(self, receipt, status, transaction_id):
        self.saved_receipts.append((receipt, status, transaction_id))

    def link(self, row_number, receipt_id, category):
        self.links.append((row_number, receipt_id, category))

    def add(self, row):
        self.added.append(row)
        return True


def desk_for(reply=BIEDRONKA):
    return ReceiptDesk(download=lambda file_id: (b"jpeg", ".jpg"), ask=fake_ask(reply))


def test_photo_linked_to_bank_payment():
    store = FakeStore([tx("mono-1", "2026-10-24 18:30", -47.13)])
    text, keyboard = desk_for().from_photo("file1", store, NOW)

    receipt, status, tx_id = store.saved_receipts[0]
    assert (status, tx_id) == ("linked", "mono-1")
    assert store.links == [(2, receipt.id, "Groceries")]
    assert keyboard is None
    assert text.startswith("Biedronka, 24 Oct, 47.13 PLN, 6 items.")
    assert "Linked to mono PLN *1111 ✓" in text
    assert "Groceries 27.66, Other 10.98, Household 8.49" in text


def test_photo_without_match_offers_cash_or_card():
    store = FakeStore()
    desk = desk_for()
    text, keyboard = desk.from_photo("file1", store, NOW)

    assert "No bank payment matches yet." in text
    assert [b["text"] for b in keyboard[0]] == ["Cash", "Card, link later"]
    assert store.saved_receipts == []

    reply = desk.answer(keyboard[0][0]["callback_data"], store)
    receipt, status, tx_id = store.saved_receipts[0]
    assert (status, tx_id) == ("cash", receipt.id)
    row = store.added[0]
    assert row["id"] == receipt.id and row["receipt_id"] == receipt.id
    assert row["amount_pln"] == -47.13 and row["account"] == "cash" and row["category"] == "Groceries"
    assert reply == "Biedronka 47.13 PLN saved as cash ✓"
    assert desk.answer(keyboard[0][0]["callback_data"], store) == receipts.EXPIRED


def test_card_choice_keeps_receipt_pending():
    store = FakeStore()
    desk = desk_for()
    _, keyboard = desk.from_photo("file1", store, NOW)

    assert (
        desk.answer(keyboard[0][1]["callback_data"], store)
        == "Biedronka: waiting for the card payment to link"
    )
    assert store.saved_receipts[0][1:] == ("pending", "")
    assert store.added == []


def test_same_receipt_twice():
    store = FakeStore()
    store.saved_receipts.append(
        (receipts.read(b"x", ".jpg", CATS, NOW.date(), ask=fake_ask(BIEDRONKA)), "cash", "")
    )
    assert desk_for().from_photo("file1", store, NOW) == ("Already have this receipt.", None)


def test_unreadable_photo_and_items_that_dont_add_up():
    store = FakeStore()
    text, _ = desk_for({"error": "not a receipt"}).from_photo("f", store, NOW)
    assert text == "That doesn't look like a receipt."

    off = {**BIEDRONKA, "total": 50.00}
    text, _ = desk_for(off).from_photo("f", store, NOW)
    assert "Check the Sheet: items add up to 47.13, not 50.00" in text


def test_claude_failure_gives_a_retry_hint():
    def broken(prompt, cwd, model, timeout):
        raise RuntimeError("timeout")

    desk = ReceiptDesk(download=lambda f: (b"x", ".jpg"), ask=broken)
    text, keyboard = desk.from_photo("f", FakeStore(), NOW)
    assert text.startswith("Couldn't read that receipt")
    assert keyboard is None
