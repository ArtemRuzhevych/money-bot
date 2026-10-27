from decimal import Decimal

import pytest

from money_bot import wallet


@pytest.fixture(autouse=True)
def card_accounts(monkeypatch):
    monkeypatch.setenv("CARD_ACCOUNTS", "millennium=millennium, pumb=pumb,пумб=pumb")


class FakeRates:
    def to_pln(self, amount, code, day):
        return amount * {"PLN": 1, "EUR": Decimal("4.25"), "UAH": Decimal("0.1")}[code]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("47,13 zł", (Decimal("47.13"), "PLN")),
        ("PLN 47.13", (Decimal("47.13"), "PLN")),
        ("€2.00", (Decimal("2.00"), "EUR")),
        ("1 234,56 zł", (Decimal("1234.56"), "PLN")),
        ("1 234,56 zł", (Decimal("1234.56"), "PLN")),
        ("1,234.56 USD", (Decimal("1234.56"), "USD")),
        ("1.234 zł", (Decimal("1234"), "PLN")),
        ("1,234 zł", (Decimal("1234"), "PLN")),
        ("120 ₴", (Decimal("120"), "UAH")),
        ("8.5", (Decimal("8.5"), "PLN")),
        (12.4, (Decimal("12.4"), "PLN")),
    ],
)
def test_parse_amount(text, expected):
    assert wallet.parse_amount(text) == expected


@pytest.mark.parametrize("text", ["", "zł", "free", "0,00 zł"])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        wallet.parse_amount(text)


def item(**kw):
    return {
        "received": "2026-10-27T17:04:30.000Z",  # the POST lands a bit later
        "card": "Millennium Visa",
        "merchant": "Biedronka",
        "amount": "47,13 zł",
        "time": "2026-10-27T18:03:00+01:00",
        **kw,
    }


def test_to_row_maps_card_amount_and_warsaw_time():
    row = wallet.to_row(item(), FakeRates())
    assert row == {
        "id": row["id"],
        "date": "2026-10-27 18:03",
        "amount": -47.13,
        "currency": "PLN",
        "amount_pln": -47.13,
        "account": "millennium",
        "merchant": "Biedronka",
        "description": "Apple Pay: Millennium Visa",
        "source": "wallet",
    }
    assert row["id"].startswith("wallet-")
    assert wallet.to_row(item(), FakeRates())["id"] == row["id"]  # a retried POST is the same payment
    assert wallet.to_row(item(merchant="Lidl"), FakeRates())["id"] != row["id"]


@pytest.mark.parametrize(
    ("card", "account"),
    [("PUMB Mastercard", "pumb"), ("ПУМБ", "pumb"), ("Revolut", "Revolut"), ("", "wallet")],
)
def test_account_from_card(card, account):
    assert wallet.to_row(item(card=card), FakeRates())["account"] == account


def test_without_card_accounts_the_card_name_is_the_account(monkeypatch):
    monkeypatch.delenv("CARD_ACCOUNTS")
    assert wallet.to_row(item(), FakeRates())["account"] == "Millennium Visa"


def test_foreign_currency_converts_and_missing_time_uses_received():
    row = wallet.to_row(item(amount="€2.00", time=""), FakeRates())
    assert (row["currency"], row["amount"], row["amount_pln"]) == ("EUR", -2.0, -8.5)
    assert row["date"] == "2026-10-27 18:04"  # 17:04 UTC is 18:04 in Warsaw


class Store:
    def __init__(self, inbox, fail=False):
        self.inbox, self.fail = list(inbox), fail
        self.saved = []

    def inbox_rows(self):
        return list(self.inbox)

    def clear_inbox(self, count):
        del self.inbox[:count]

    def add(self, row):
        if self.fail:
            raise OSError("Sheets API down")
        if any(r["id"] == row["id"] for r in self.saved):
            return False
        self.saved.append(row)
        return True


def test_pull_saves_payments_and_clears_them():
    store = Store([item(), item(merchant="Lidl"), item()])
    added, bad = wallet.pull(store, FakeRates())

    assert (added, bad) == (2, [])
    assert [r["merchant"] for r in store.saved] == ["Biedronka", "Lidl"]
    assert store.inbox == []


def test_unreadable_payment_is_reported_and_cleared():
    store = Store([item(amount="free"), item()])
    added, bad = wallet.pull(store, FakeRates())

    assert added == 1
    assert bad == ["Biedronka free (Millennium Visa)"]
    assert store.inbox == []


def test_nothing_is_cleared_when_saving_fails():
    store = Store([item()], fail=True)
    with pytest.raises(OSError):
        wallet.pull(store, FakeRates())
    assert len(store.inbox) == 1


def test_empty_inbox_does_nothing():
    store = Store([])
    assert wallet.pull(store, FakeRates()) == (0, [])
